"""Voice intent handlers.

These replace eight `intent_script:` entries, and the point of moving them is not
tidiness. An intent_script renders its `speech` AFTER running its `action` and cannot
see variables set there, so every outcome had to be smuggled through the state of an
`input_text` helper - which silently truncates at its `max`, and once made a successful
skip read back to the user as a failure. A handler returns its own response from the
value it just computed, so that channel does not need to exist.

The same limitation forced the time arithmetic to be written twice per intent, once in
`action` and once in `speech`, as a ~1200-character single-line Jinja expression. Here
it is one function.

The sentences stay in `custom_sentences/en/reminders.yaml`. Home Assistant matches those
to an intent NAME and then looks up whoever registered it, so the grammar - the part that
took longest to get right - is untouched by this move.
"""

from __future__ import annotations

import asyncio
import logging
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, intent
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ANNOUNCE_TARGETS,
    CONF_NAME,
    CONF_CONFIRM_CANCEL,
    CONF_AFTERNOON_TIME,
    CONF_DEFAULT_TIME,
    CONF_EVENING_TIME,
    CONF_MORNING_TIME,
    DEFAULT_CONFIRM_CANCEL,
    DEFAULT_AFTERNOON_TIME,
    DEFAULT_EVENING_TIME,
    DEFAULT_MORNING_TIME,
    DEFAULT_TIME,
    DOMAIN,
    RESULT_NONE_LEFT,
    RESULT_NOT_RECURRING,
    RESULT_OK,
)
from .store import InvalidRule, PastDue

_LOGGER = logging.getLogger(__name__)

def _lists(hass) -> list:
    from . import all_managers

    return all_managers(hass)


DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

# assist_satellite.ask_question opens a NEW conversation on the satellite, which will
# not start while the turn that triggered it is still open. So a confirmation runs
# detached, after a pause long enough for the calling turn to have closed.
ASK_SETTLE = 2

# "Who's this reminder for?" is asked until it is ANSWERED, not until a clock runs out:
# assist_satellite.ask_question waits on the pipeline turn, and a turn that ends in
# silence comes back as an error rather than as an answer. Silence is not consent to
# file a reminder on someone else's list, so it is asked again. What bounds this is a
# count of spoken attempts, never a deadline - a slow answer is never punished, and a
# speaker in an empty house still stops eventually.
ASK_REPEATS = 3

# How close a mis-heard name has to be to count. "Jenn" for "Jen" yes; "james" for
# anything, no - and a wrong guess here puts a reminder on someone else's list.
NAME_SIMILARITY = 0.7

YES_SENTENCES = [
    "yes", "yeah", "yep", "yes please", "do it", "go ahead", "confirm", "correct",
    "cancel it",
]
NO_SENTENCES = [
    "no", "nope", "no thanks", "don't", "do not", "leave it", "never mind",
    "nevermind", "stop", "keep it",
]


# ---------------------------------------------------------------- slot reading


def _slot(slots: dict, key: str, default=None):
    """One slot's VALUE. hassil hands over {"value": x, "text": ...} per slot.

    The value is what a range or values list resolved to - an int for `hour`, "pm" for
    "in the evening" - while the text is what was said. Reading the text instead is the
    mistake the debug endpoint exists to catch.
    """
    raw = slots.get(key)
    if raw is None:
        return default
    value = raw.get("value") if isinstance(raw, dict) else raw
    return default if value is None else value


def _int(slots: dict, key: str, default: int | None = None) -> int | None:
    """A slot as an int. Sentence-level slots arrive as strings ("90"), lists as ints."""
    value = _slot(slots, key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(slots: dict, key: str, default: str = "") -> str:
    value = _slot(slots, key, default)
    return str(value).strip() if value is not None else default


# A time of day, said instead of a clock time. "every Sunday night" used to match the
# ONE-OFF intent with "every Sunday night" swallowed into the reminder text, which is
# worse than a refusal: it produced a silently wrong reminder.
#
# morning is absent on purpose - it falls through to the configured default time, so
# "every Sunday morning" and "every Sunday" agree with each other.
# What a part of the day means when nothing is configured. Morning is deliberately
# absent: with no entry here it falls through to the plain default, which is what
# "tomorrow morning" has always done.
_DAYPART_TIME = {"afternoon": (14, 0), "evening": (19, 0), "night": (21, 0)}


class Times(tuple):
    """The default (hour, minute), which also knows what each part of the day means.

    It IS a plain tuple, so every existing call, unpacking and test carries on working
    unchanged - the daypart table rides along instead of being threaded through six
    signatures as a second argument that almost every caller would only be forwarding.
    """

    def __new__(cls, default, dayparts=None):
        obj = super().__new__(cls, tuple(default))
        obj.parts = dict(dayparts or {})
        return obj

    def of(self, daypart: str | None) -> tuple[int, int]:
        return self.parts.get(daypart) or _DAYPART_TIME.get(daypart) or tuple(self)


def _daypart_time(daypart: str | None, default_time) -> tuple[int, int]:
    """The time a part of the day means. A bare tuple falls back to the built-in table."""
    if isinstance(default_time, Times):
        return default_time.of(daypart)
    return _DAYPART_TIME.get(daypart) or tuple(default_time)
_DAYPART_AMPM = {"morning": "am", "afternoon": "pm", "evening": "pm", "night": "pm"}


def _resolve_time(slots: dict, default_time: tuple[int, int]) -> tuple[int, int]:
    """(hour, minute) in 24h, from the clock, or the time of day, or the default.

    A time of day also disambiguates a bare hour: "every Sunday night at 9" is 21:00, not
    09:00, without the speaker having to say "pm".
    """
    daypart = _text(slots, "daypart")
    hour = _int(slots, "hour")
    if hour is None:
        return _daypart_time(daypart, default_time)
    ampm = _text(slots, "ampm") or _DAYPART_AMPM.get(daypart, "")
    return _hour24(hour, ampm), _int(slots, "minute", 0)


# ---------------------------------------------------------------- time arithmetic


def _hour24(hour: int, ampm: str, noon_for_bare_12: bool = False) -> int:
    """12-hour clock to 24.

    `noon_for_bare_12` is the cancel-by-time rule: people say "cancel my reminder at 12"
    meaning midday, so a bare 12 is noon rather than midnight. Scheduling does not do
    this, and the difference is deliberate - it is preserved from the YAML version rather
    than unified, because unifying it would change what an existing phrase means.
    """
    if noon_for_bare_12 and hour == 12 and ampm != "am":
        return 12
    return (hour % 12) + (12 if ampm == "pm" else 0)


def when_from_slots(now: datetime, slots: dict,
                    default_time: tuple[int, int] = (8, 15)) -> datetime | None:
    """The single point in time a "remind me ..." sentence asked for.

    Four shapes, checked in the order the grammar can produce them: tomorrow morning,
    a named weekday, a relative offset, and a clock time today or N days out.
    """
    if _slot(slots, "tomorrow_morning") is not None:
        d_hour, d_minute = _daypart_time("morning", default_time)
        base = now if now.hour < d_hour else now + timedelta(days=1)
        return base.replace(hour=d_hour, minute=d_minute, second=0, microsecond=0)

    weekday = _int(slots, "weekday")
    if weekday is not None:
        h24, minute = _resolve_time(slots, default_time)
        delta = (weekday - now.weekday()) % 7
        when = (now + timedelta(days=delta)).replace(
            hour=h24, minute=minute, second=0, microsecond=0
        )
        # Today already gone means they meant next week, not an hour ago.
        return when if when > now else when + timedelta(days=7)

    rel_num = _int(slots, "rel_num")
    if rel_num is not None:
        unit = _text(slots, "rel_unit", "minutes")
        step = {
            "seconds": timedelta(seconds=rel_num),
            "minutes": timedelta(minutes=rel_num),
            "hours": timedelta(hours=rel_num),
            "days": timedelta(days=rel_num),
        }.get(unit)
        if step is None:
            return None
        return now + step

    if _int(slots, "hour") is None and not _slot(slots, "daypart"):
        return None
    h24, minute = _resolve_time(slots, default_time)
    offset = _int(slots, "day_offset", 0)
    when = (now + timedelta(days=offset)).replace(
        hour=h24, minute=minute, second=0, microsecond=0
    )
    # A bare clock time that has passed means tomorrow. An explicit day does not roll.
    return when if (offset > 0 or when > now) else when + timedelta(days=1)


def when_at_slots(now: datetime, slots: dict) -> datetime | None:
    """The time a "cancel my reminder at ..." sentence named.

    Unlike scheduling this does NOT roll a past time forward to tomorrow: naming a time
    that has gone by should find nothing, not silently cancel tomorrow's reminder.
    """
    hour = _int(slots, "hour")
    if hour is None:
        return None
    ampm = _text(slots, "ampm") or _DAYPART_AMPM.get(_text(slots, "daypart"), "")
    h24 = _hour24(hour, ampm, noon_for_bare_12=True)
    minute = _int(slots, "minute", 0)

    weekday = _int(slots, "weekday")
    if weekday is not None:
        delta = (weekday - now.weekday()) % 7
        when = (now + timedelta(days=delta)).replace(
            hour=h24, minute=minute, second=0, microsecond=0
        )
        return when if when > now else when + timedelta(days=7)

    offset = _int(slots, "day_offset", 0)
    return (now + timedelta(days=offset)).replace(
        hour=h24, minute=minute, second=0, microsecond=0
    )


def recurrence_from_slots(now: datetime, slots: dict, extra_bound: str = "",
                          default_time: tuple[int, int] = (8, 15),
                          ) -> tuple[datetime, str, str] | None:
    """First occurrence, RRULE and prose for a "remind me every ..." sentence.

    Only the FIRST occurrence is worked out here; the rule describes the rest. It is
    found by scanning forward a day at a time rather than by month arithmetic, which is
    one uniform loop for all five kinds and cannot construct 31 November the way a
    replace(day=31) would.
    """
    mode = _text(slots, "recur") or _text(slots, "recur_kind")
    if not mode:
        return None
    h24, minute = _resolve_time(slots, default_time)
    monthday = _int(slots, "monthday", 1)
    weekday = _int(slots, "weekday", 0)

    first = None
    for i in range(400):
        day = now + timedelta(days=i)
        matches = (
            mode == "daily"
            or (mode == "weekly" and day.weekday() == weekday)
            or (mode == "weekdays" and day.weekday() < 5)
            or (mode == "weekends" and day.weekday() >= 5)
            or (mode == "monthly" and day.day == monthday)
        )
        if not matches:
            continue
        candidate = day.replace(hour=h24, minute=minute, second=0, microsecond=0)
        if candidate > now:
            first = candidate
            break
    if first is None:
        return None

    if mode == "daily":
        rrule, every = "FREQ=DAILY", "every day"
    elif mode == "weekly":
        rrule = f"FREQ=WEEKLY;BYDAY={BYDAY[weekday]}"
        every = f"every {DAY_NAMES[weekday]}"
    elif mode == "weekdays":
        rrule, every = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "every weekday"
    elif mode == "weekends":
        rrule, every = "FREQ=WEEKLY;BYDAY=SA,SU", "every weekend day"
    else:
        rrule = f"FREQ=MONTHLY;BYMONTHDAY={monthday}"
        every = f"on the {monthday}{_ordinal(monthday)} of every month"

    # A bound from the sentence's own slots wins over one recovered from the text; both
    # cannot sensibly apply, and the slots are the less ambiguous of the two.
    bound = bound_fragment(
        now,
        number=_int(slots, "bound_num"),
        unit=_UNIT_WORDS.get(_text(slots, "bound_unit").lower()) if _slot(slots, "bound_unit") else None,
        month=_int(slots, "until_month"),
        day=_int(slots, "until_day"),
    ) or extra_bound
    rrule += bound
    return first, rrule, every


# ---------------------------------------------------------------- bounds

_UNIT_WORDS = {
    "day": "days", "days": "days",
    "week": "weeks", "weeks": "weeks",
    "month": "months", "months": "months",
    "time": "times", "times": "times",
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30,
    "a": 1, "an": 1, "couple": 2, "few": 3,
}
_MONTH_INDEX = {m.lower(): i + 1 for i, m in enumerate(MONTH_NAMES)}
_MONTH_INDEX.update({m.lower()[:3]: i + 1 for i, m in enumerate(MONTH_NAMES)})
_MONTH_INDEX["sept"] = 9

# The bound as it may appear at the END of the captured text. It lands there whenever the
# sentence puts the task last - "remind me every Sunday at 9 pm to water the ferns for the
# next 6 weeks" - because {reminder_text} is a wildcard and swallows everything after it.
# The grammar cannot take the bound off it: an optional group following a trailing
# wildcard is simply absorbed. So it is parsed back off here instead.
_TAIL = re.compile(
    r"""[\s,]*(?:
        for\s+(?:the\s+)?next\s+(?P<n1>\d+|[a-z]+)\s+(?P<u1>days?|weeks?|months?)
      | for\s+(?P<n2>\d+|[a-z]+)\s+(?P<u2>days?|weeks?|months?)
      | (?P<n3>\d+|[a-z]+)\s+(?:more\s+)?times?
      | until\s+(?P<mon>[a-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _number(word) -> int | None:
    if word is None:
        return None
    text = str(word).strip().lower()
    if text.isdigit():
        return int(text)
    return _NUMBER_WORDS.get(text)


def _add_months(when: datetime, months: int) -> datetime:
    """Calendar months, clamped. Adding one to 31 January lands on 28/29 February."""
    month = when.month - 1 + months
    year = when.year + month // 12
    month = month % 12 + 1
    day = min(when.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return when.replace(year=year, month=month, day=day)


def _end_of_day(when: datetime) -> datetime:
    return when.replace(hour=23, minute=59, second=59, microsecond=0)


def _until_date(now: datetime, month: int, day: int) -> datetime:
    """The next occurrence of that month and day - this year if it is still ahead."""
    candidate = now.replace(month=month, day=day)
    if candidate.date() < now.date():
        candidate = candidate.replace(year=now.year + 1)
    return _end_of_day(candidate)


def bound_fragment(now: datetime, count=None, number=None, unit=None,
                   month=None, day=None) -> str:
    """The ";COUNT=n" or ";UNTIL=..." to hang off a rule. Empty when unbounded.

    A duration becomes UNTIL rather than COUNT, and the difference is not cosmetic:
    "every day for the next 2 weeks" is fourteen occurrences, not two. They also diverge
    the moment one is skipped - COUNT spends one of its n, UNTIL keeps the end date -
    and "for the next 6 weeks" plainly means the date.
    """
    if count:
        return f";COUNT={int(count)}"
    if month and day:
        return ";UNTIL=" + _until_date(now, int(month), int(day)).strftime("%Y%m%dT%H%M%S")
    if number and unit:
        n, u = int(number), str(unit)
        if u == "times":
            return f";COUNT={n}"
        if u == "days":
            end = now + timedelta(days=n)
        elif u == "weeks":
            end = now + timedelta(weeks=n)
        else:
            end = _add_months(now, n)
        return ";UNTIL=" + _end_of_day(end).strftime("%Y%m%dT%H%M%S")
    return ""


def split_trailing_bound(text: str, now: datetime) -> tuple[str, str]:
    """Pull a bound off the end of captured text. Returns (text, rrule fragment)."""
    match = _TAIL.search(text or "")
    if not match:
        return text, ""
    g = match.groupdict()
    number = _number(g["n1"] or g["n2"] or g["n3"])
    if number is None and not g["mon"]:
        return text, ""
    unit = _UNIT_WORDS.get((g["u1"] or g["u2"] or "").lower()) if (g["u1"] or g["u2"]) else None
    if g["n3"] and not unit:
        unit = "times"
    month = _MONTH_INDEX.get((g["mon"] or "").lower()) if g["mon"] else None
    if g["mon"] and month is None:
        return text, ""
    fragment = bound_fragment(now, number=number, unit=unit, month=month, day=g["day"])
    if not fragment:
        return text, ""
    return text[: match.start()].rstrip(" ,"), fragment


# ---------------------------------------------------------------- the edit follow-up

_WEEKDAY_WORDS = {d.lower(): i for i, d in enumerate(DAY_NAMES)}
_RECUR_WORDS = {
    "day": "daily", "days": "daily",
    "weekday": "weekdays", "weekdays": "weekdays",
    "weekend": "weekends", "weekends": "weekends",
}
_WD = "|".join(_WEEKDAY_WORDS)
_DAYPARTS = "morning|afternoon|evening|night"

# Three ways to say which half you are changing. Without one, the answer is read for
# whether it contains a time at all - which works, but cannot tell "Watch Lanterns at
# 9 pm" meant as a NAME from the same words meant as a name and a time.
_ANSWER_MODES = (
    (re.compile(r"^\s*(?:change|set|update|fix)\s+(?:the\s+|its\s+)?"
                r"(?:timing|time|schedule|when)\s+(?:to|for)\s+", re.I), "timing"),
    (re.compile(r"^\s*(?:change|set|update|fix)\s+(?:the\s+|its\s+)?"
                r"(?:reminder|text|name|wording|title)\s+(?:to say|to|into)\s+", re.I), "text"),
    (re.compile(r"^\s*(?:rename|retitle)\s+(?:it\s+)?(?:to|as)\s+", re.I), "text"),
    (re.compile(r"^\s*replace\s+(?:it\s+)?with\s+", re.I), "both"),
)

# Pieces of a time expression, found anywhere in the phrase rather than in a fixed
# order - "10 pm Sundays" and "Sundays at 10 pm" are the same request.
_RE_REL = re.compile(
    r"\bin\s+(?P<num>\d+|[a-z]+)\s+(?P<unit>seconds?|secs?|minutes?|mins?|hours?|days?)\b", re.I)
_RE_EVERY = re.compile(
    rf"\bevery\s+(?:(?P<wd>{_WD})s?|(?P<kind>days?|weekdays?|weekends?)|(?P<dp>{_DAYPARTS}))\b", re.I)
# A plural weekday IS a recurrence: "Sundays" means every Sunday.
_RE_PLURAL_WD = re.compile(rf"\b(?P<wd>{_WD})s\b", re.I)
_RE_DAILY = re.compile(r"\b(?:daily|nightly)\b", re.I)
# Compact first: "830 pm" would otherwise be read as the hour 8 with 30 left over.
_RE_COMPACT = re.compile(r"\b(?:at\s+)?(?P<h>\d{1,2})(?P<m>\d{2})\s*(?P<ap>[ap]\.?\s?m\.?)\b", re.I)
_RE_CLOCK = re.compile(
    r"\b(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?\s?m\.?)\b"
    r"|\bat\s+(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\b", re.I)
_RE_DAYWORD = re.compile(r"\b(?P<day>today|tonight|tomorrow)\b", re.I)
_RE_ONE_WD = re.compile(rf"\b(?:on\s+)?(?P<wd>{_WD})\b", re.I)
_RE_DAYPART = re.compile(rf"\b(?P<dp>{_DAYPARTS})\b", re.I)
_LEAD_TAIL = re.compile(r"[\s,]*\b(?:to|for|on|at|is|it|starting|start)\s*$", re.I)


def _clean(slots: dict) -> dict:
    """Drop empty slots and wrap the rest the way hassil hands them over."""
    return {k: {"value": v} for k, v in slots.items() if v not in (None, "")}


def _ampm(raw: str | None) -> str:
    if not raw:
        return ""
    return "pm" if raw.lower().replace(".", "").replace(" ", "").startswith("p") else "am"


def _read_time(phrase: str, now: datetime, default_time: tuple[int, int]):
    """Pull a time expression out of a phrase.

    Returns (lead text, due, rrule). Every piece is optional and order does not matter,
    because speech does not keep one. The captured pieces become the same slots the voice
    grammar produces and go to the SAME arithmetic - two implementations of "every
    Thursday at 7" would not stay in step.
    """
    rest, bound = split_trailing_bound(phrase, now)
    slots: dict = {}
    first = len(rest)
    found = False

    def take(match, index_from=None):
        nonlocal first, found
        found = True
        first = min(first, match.start() if index_from is None else index_from)

    if m := _RE_REL.search(rest):
        number = _number(m.group("num"))
        unit = {"sec": "seconds", "second": "seconds", "min": "minutes", "minute": "minutes",
                "hour": "hours", "day": "days"}.get(m.group("unit").lower().rstrip("s"))
        if number is not None and unit:
            take(m)
            when = when_from_slots(now, _clean({"rel_num": number, "rel_unit": unit}), default_time)
            return rest[:first].strip(), when, None

    if m := _RE_EVERY.search(rest):
        take(m)
        if m.group("wd"):
            slots["recur"] = "weekly"
            slots["weekday"] = _WEEKDAY_WORDS[m.group("wd").lower()]
        elif m.group("kind"):
            slots["recur_kind"] = _RECUR_WORDS[m.group("kind").lower()]
        else:
            slots["recur_kind"] = "daily"
            slots["daypart"] = m.group("dp").lower()
    elif m := _RE_PLURAL_WD.search(rest):
        take(m)
        slots["recur"] = "weekly"
        slots["weekday"] = _WEEKDAY_WORDS[m.group("wd").lower()]
    elif m := _RE_DAILY.search(rest):
        take(m)
        slots["recur_kind"] = "daily"
        if m.group(0).lower() == "nightly":
            slots["daypart"] = "night"

    if m := _RE_COMPACT.search(rest):
        take(m)
        slots.update(hour=m.group("h"), minute=m.group("m"), ampm=_ampm(m.group("ap")))
    elif m := _RE_CLOCK.search(rest):
        take(m)
        slots.update(hour=m.group("h") or m.group("h2"),
                     minute=m.group("m") or m.group("m2"),
                     ampm=_ampm(m.group("ap")))

    if "recur" not in slots and "recur_kind" not in slots:
        if m := _RE_DAYWORD.search(rest):
            take(m)
            day = m.group("day").lower()
            if day == "tomorrow":
                slots["day_offset"] = 1
            elif day == "tonight" and not slots.get("ampm"):
                slots["ampm"] = "pm"
        elif m := _RE_ONE_WD.search(rest):
            take(m)
            slots["weekday"] = _WEEKDAY_WORDS[m.group("wd").lower()]

    if not slots.get("daypart"):
        if m := _RE_DAYPART.search(rest):
            take(m)
            slots["daypart"] = m.group("dp").lower()

    if not found or not slots:
        return rest.strip(), None, None

    lead = _LEAD_TAIL.sub("", rest[:first]).strip(" ,")
    if "recur" in slots or "recur_kind" in slots:
        got = recurrence_from_slots(now, _clean(slots), bound, default_time)
        return (lead, got[0], got[1]) if got else (lead, None, None)
    return lead, when_from_slots(now, _clean(slots), default_time), None


def parse_edit_answer(
    sentence: str, now: datetime, default_time: tuple[int, int]
) -> tuple[str | None, datetime | None, str | None]:
    """Read the spoken answer into (new text, new due, new rrule). None means unchanged.

    "change the timing to ...", "change the reminder to ..." and "replace with ..." say
    outright which half is being changed. Without one of them the answer is read for
    whether it contains a time at all, which is right most of the time and cannot be
    right always - "Watch Lanterns at 9 pm" is a perfectly good NAME.
    """
    said = (sentence or "").strip().rstrip(".!?").strip()
    if not said:
        return None, None, None

    mode = "infer"
    for pattern, name in _ANSWER_MODES:
        if pattern.match(said):
            said = pattern.sub("", said, count=1).strip()
            mode = name
            break
    said = said.strip().strip('"“”').strip()
    if not said:
        return None, None, None

    if mode == "text":
        return said, None, None

    lead, due, rrule = _read_time(said, now, default_time)
    if mode == "timing":
        # Anything before the time was filler ("it to", "this one"), not a new name.
        return None, due, rrule
    if due is None and rrule is None:
        return said, None, None
    return (lead or None), due, rrule


def _ordinal(n: int) -> str:
    if n in (1, 21, 31):
        return "st"
    if n in (2, 22):
        return "nd"
    if n in (3, 23):
        return "rd"
    return "th"


# ---------------------------------------------------------------- speech helpers


def clock(when: datetime) -> str:
    """"7:15 AM". Built by hand rather than with %-I, which is not portable."""
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def day_phrase(when: datetime, now: datetime) -> str:
    """"", " tomorrow" or " on Thursday" - the tail of "at 7:15 AM ...."."""
    if when.date() == now.date():
        return ""
    if when.date() == (now + timedelta(days=1)).date():
        return " tomorrow"
    return f" on {DAY_NAMES[when.weekday()]}"


def starting_phrase(when: datetime, now: datetime) -> str:
    """When a recurring reminder starts: today, tomorrow, a weekday, or a date."""
    if when.date() == now.date():
        return "today"
    if when.date() == (now + timedelta(days=1)).date():
        return "tomorrow"
    if (when.date() - now.date()).days < 7:
        return DAY_NAMES[when.weekday()]
    return f"{MONTH_NAMES[when.month - 1]} {when.day}"


def _read_hm(raw) -> tuple[int, int] | None:
    """A configured "HH:MM:SS" as (hour, minute). None when it is missing or unreadable."""
    if not raw:
        return None
    parsed = dt_util.parse_time(raw) if isinstance(raw, str) else raw
    return (parsed.hour, parsed.minute) if parsed else None


_RE_LEAD_TO = re.compile(r"^\s*(?:to|about|that i|me to)\s+", re.I)


def _task_only(rest: str) -> str:
    """The task out of what the fallback captured. "to walk the dog" -> "walk the dog"."""
    return _RE_LEAD_TO.sub("", (rest or "").strip()).strip()


def describe(reminder, now: datetime) -> str:
    """One reminder as a phrase, for lists and confirmations.

    The bound goes AFTER the time - "every day at 8:15 AM, until September 8" - because
    the other order reads as though the ending were part of the clock.
    """
    if reminder.recurring:
        return (f"{reminder.text} {reminder.every_base} at {clock(reminder.due)}"
                f"{reminder.every_bound}")
    return f"{reminder.text} at {clock(reminder.due)}{day_phrase(reminder.due, now)}"


# ---------------------------------------------------------------- base handler


class ReminderIntent(intent.IntentHandler):
    """Shared plumbing: find the manager, and answer in one line."""

    def _manager(self, hass: HomeAssistant):
        """The default list. Used where whose list it is does not change the answer."""
        from . import sole_manager

        return sole_manager(hass)

    def _route(self, hass: HomeAssistant, intent_obj) -> tuple:
        """Which list this request means, and whether that was a guess.

        The ladder, in order:
          1. a name in the sentence  - "remind Jen to ..." works from any satellite
          2. the satellite that heard it, when exactly one list claims it
          3. nothing - the caller decides whether to ask or fall back to the default

        Usernames are deliberately absent: a spoken request carries NO user at all
        (measured - a wake-word pipeline run has no session behind it), so routing on
        one would work from a browser and silently fail from every speaker in the house.
        """
        from . import all_managers, manager_named

        managers = all_managers(hass)
        if not managers:
            return None, False
        if len(managers) == 1:
            return managers[0], False

        if spoken := _text(intent_obj.slots, "person"):
            named = manager_named(hass, spoken)
            return (named, False) if named else (None, True)

        device_id = getattr(intent_obj, "device_id", None)
        if device_id:
            registry = er.async_get(hass)
            owned = [
                m
                for entry in er.async_entries_for_device(registry, device_id)
                if entry.domain == "assist_satellite"
                for m in managers
                if m.owns_satellite(entry.entity_id)
            ]
            unique = {m.entry.entry_id: m for m in owned}
            if len(unique) == 1:
                return next(iter(unique.values())), False

        # Shared, or unknown. Ambiguous, and the caller decides what that costs.
        return None, True

    def _default_time(self, manager) -> tuple[int, int]:
        """The configured default time, for a reminder that names no clock time.

        This option existed in the config flow from the start but was never read: the
        grammar supplied hour/minute/ampm slots on the no-time sentences, so the handler
        could not tell "the user said nothing" from "the user said 8:15". Those slots are
        gone, which is what makes the setting real.
        """
        parts = {}
        for daypart, key, fallback in (
            ("morning", CONF_MORNING_TIME, DEFAULT_MORNING_TIME),
            ("afternoon", CONF_AFTERNOON_TIME, DEFAULT_AFTERNOON_TIME),
            # Both words read the one setting.
            ("evening", CONF_EVENING_TIME, DEFAULT_EVENING_TIME),
            ("night", CONF_EVENING_TIME, DEFAULT_EVENING_TIME),
        ):
            if hm := _read_hm(manager._opt(key, fallback)):
                parts[daypart] = hm
        return Times(_read_hm(manager._opt(CONF_DEFAULT_TIME, DEFAULT_TIME)) or (8, 15), parts)

    def _default_time_phrase(self, manager) -> str:
        """The plain default as a spoken clock time - "8:15 AM"."""
        hour, minute = self._default_time(manager)
        return clock(dt_util.now().replace(hour=hour, minute=minute))

    def _confirms(self, manager) -> bool:
        """Whether a destructive change is read back and waited on."""
        return bool(manager._opt(CONF_CONFIRM_CANCEL, DEFAULT_CONFIRM_CANCEL))

    def _routed_manager(self, hass: HomeAssistant, intent_obj) -> tuple:
        """(the list this request means, a refusal to speak instead).

        Routed where the ladder can settle it, the default otherwise - routing can only
        ever narrow to the right list, and where it cannot, the fallback is the same
        default that was used before routing existed. The one case that must NOT fall
        back is a name that matches no list: answering about John's reminders when
        someone asked about Jen's is worse than saying there is no such list.
        """
        spoken = _text(intent_obj.slots, "person")
        routed, ambiguous = self._route(hass, intent_obj)
        if routed is None and spoken and _lists(hass):
            return None, f"I don't have a list for {spoken}.", False
        return routed or self._manager(hass), None, ambiguous

    def _respond(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Answer a question for the right list, asking first when it could mean two.

        Every read intent is this same four-step shape, so it lives here once and each
        one supplies only `_answer`.
        """
        hass = intent_obj.hass
        manager, refusal, ambiguous = self._routed_manager(hass, intent_obj)
        if refusal:
            return self._say(intent_obj, refusal)
        if ambiguous:
            asked = self._ask_whose_read(
                hass, intent_obj, lambda m: self._answer(hass, m, intent_obj)
            )
            if asked is not None:
                return asked
        if manager is None:
            return self._say(intent_obj, self._not_ready(hass))
        return self._say(intent_obj, self._answer(hass, manager, intent_obj))

    def _answer(self, hass: HomeAssistant, manager, intent_obj) -> str:
        """What to say for this list. Read intents override this; nothing else uses it."""
        raise NotImplementedError

    def _whose(self, hass: HomeAssistant, manager) -> str | None:
        """The name to put in a reply, or None when "you" is right.

        With one list "you" is the natural word and naming its owner is odd. With
        several it is the only way to know whose answer you just got - which matters
        most exactly when the list was picked for you rather than named by you.
        """
        return manager.name if len(_lists(hass)) > 1 else None

    def _ask_whose_read(self, hass: HomeAssistant, intent_obj, answer):
        """Ask whose reminders, then answer. None means there was no way to ask.

        On a satellite two lists share, "how many reminders do I have" has two right
        answers, and picking one silently is how you get confidently told someone else's
        business. Only reached when the ladder could not settle it, so the question is
        asked in the shared room and never in a private one.
        """
        managers = _lists(hass)
        satellite = self._any_satellite(hass, managers, intent_obj)
        if satellite is None or len(managers) < 2:
            return None
        hass.async_create_background_task(
            _ask_whose_then_answer(hass, satellite, managers, answer),
            name="reminders_ask_whose_read",
        )
        return self._say(intent_obj, "")

    def _ask_whose(self, hass, intent_obj, text, due, rrule):
        """Ask whose list, if there is a satellite to ask on. None means could not ask.

        Only reached when the ladder could not resolve it, so the question is asked in
        the kitchen and never in the bedroom.
        """
        managers = _lists(hass)
        satellite = self._any_satellite(hass, managers, intent_obj)
        if satellite is None or len(managers) < 2:
            return None
        hass.async_create_background_task(
            _ask_whose_then_schedule(hass, satellite, managers, text, due, rrule),
            name="reminders_ask_whose",
        )
        return self._say(intent_obj, "")

    def _not_ready(self, hass: HomeAssistant) -> str:
        """Why there is no one list to act on.

        Three reasons, and they need different answers: still booting (temporary),
        nothing set up (a misconfiguration), or more than one list and nothing in the
        request said which - which is not a fault at all, just a missing word.
        """
        if len(_lists(hass)) > 1:
            return self._which_list(hass)
        if not hass.is_running:
            return "Hold on, I'm still wakin' up."
        return "Reminders ain't set up."

    def _which_list(self, hass: HomeAssistant) -> str:
        names = " or ".join(sorted(m.name for m in _lists(hass)))
        return f"Whose list do you mean - {names}? Say a name and I'll sort it."

    def _say(self, intent_obj: intent.Intent, speech: str) -> intent.IntentResponse:
        response = intent_obj.create_response()
        response.async_set_speech(speech)
        return response

    def _any_satellite(self, hass: HomeAssistant, managers: list, intent_obj) -> str | None:
        """The speaker this came from, if ANY list would talk to it.

        Checking only the default list is wrong for exactly the case this is for: the
        question is asked because two lists share a speaker, and neither of them need be
        the default one.
        """
        for manager in managers:
            if satellite := self._satellite(hass, manager, intent_obj):
                return satellite
        return None

    def _satellite(self, hass: HomeAssistant, manager, intent_obj) -> str | None:
        """The satellite this request came FROM, if a question may be asked back on it.

        Taken from the intent's own device_id, not from a sensor naming whoever spoke
        last. That distinction is load-bearing and it is the whole reason this belongs
        in Python: a template can only read the sensor, and the sensor stays latched on
        the last satellite that spoke - including one that an ANNOUNCE just woke, which
        no one is standing in front of. A typed or REST request then looks like a voice
        request, and the reply goes out as a spoken question to an empty room while the
        caller gets silence. device_id is absent for anything that is not a device, so
        it cannot make that mistake.

        The satellite must also be one of the configured announce targets - the same
        list that decides where a reminder speaks - so a question is never asked
        somewhere a reminder would not be.
        """
        device_id = getattr(intent_obj, "device_id", None)
        if not device_id:
            return None
        targets = manager._opt(CONF_ANNOUNCE_TARGETS) or []
        if not targets:
            return None
        registry = er.async_get(hass)
        for entry in er.async_entries_for_device(registry, device_id):
            if entry.domain != "assist_satellite" or entry.entity_id not in targets:
                continue
            state = hass.states.get(entry.entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            return entry.entity_id
        return None


# ---------------------------------------------------------------- scheduling


class SetReminderIntent(ReminderIntent):
    """remind me to X at/in ..."""

    intent_type = "CustomReminderSet"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        lists = _lists(hass)
        if not lists:
            return self._say(intent_obj, self._not_ready(hass))
        # Whose list this is may not be settled yet, and the times are per-list. Reading
        # them off the first list is only ever visible when the sentence named no time at
        # all AND two lists disagree about what the default hour is.
        manager = self._manager(hass) or lists[0]

        slots = intent_obj.slots
        text = _text(slots, "reminder_text")
        if not text:
            return self._say(intent_obj, "Beg pardon, I didn't catch what to remind you about.")

        now = dt_util.now()
        when = when_from_slots(now, slots, self._default_time(manager))
        if when is None:
            return self._say(intent_obj, "Beg pardon, I couldn't work out when.")
        if when <= now:
            return self._say(intent_obj, "That time has already passed.")

        routed, ambiguous = self._route(hass, intent_obj)
        if ambiguous:
            asked = self._ask_whose(hass, intent_obj, text, when, None)
            if asked is not None:
                return asked
        target = routed or self._manager(hass)
        if target is None:
            return self._say(intent_obj, self._which_list(hass))
        try:
            target.schedule(text, when)
        except PastDue:
            return self._say(intent_obj, "That time has already passed.")
        whose = f" {self._whose(hass, target) or 'you'}"
        return self._say(
            intent_obj,
            f"Alright, I'll remind{whose} to {text} at {clock(when)}{day_phrase(when, now)}.",
        )


class RecurringReminderIntent(ReminderIntent):
    """remind me to X every ..."""

    intent_type = "CustomReminderRecurring"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        lists = _lists(hass)
        if not lists:
            return self._say(intent_obj, self._not_ready(hass))
        # See SetReminderIntent: the times are read before whose list is known.
        manager = self._manager(hass) or lists[0]

        slots = intent_obj.slots
        text = _text(slots, "reminder_text")
        if not text:
            return self._say(intent_obj, "Beg pardon, I didn't catch what to remind you about.")

        now = dt_util.now()
        text, tail_bound = split_trailing_bound(text, now)
        if not text:
            return self._say(intent_obj, "Beg pardon, I didn't catch what to remind you about.")

        found = recurrence_from_slots(now, slots, tail_bound, self._default_time(manager))
        if found is None:
            return self._say(intent_obj, "Beg pardon, I couldn't work out when to start that one.")
        when, rrule, every = found

        routed, ambiguous = self._route(hass, intent_obj)
        if ambiguous:
            asked = self._ask_whose(hass, intent_obj, text, when, rrule)
            if asked is not None:
                return asked
        manager = routed or self._manager(hass)
        if manager is None:
            return self._say(intent_obj, self._which_list(hass))
        try:
            reminder = manager.schedule(text, when, rrule)
        except PastDue:
            return self._say(intent_obj, "That time has already passed.")
        except InvalidRule:
            _LOGGER.exception("reminders: refused rule for %r", text)
            return self._say(intent_obj, "Beg pardon, I can't repeat one that often.")

        # The store already turns a bound into prose for the to-do row and the card, so
        # the reply reads it back from there rather than describing it a second way.
        ending = reminder.every[len(every):] if reminder.every.startswith(every) else ""
        whose = self._whose(hass, manager) or "you"
        return self._say(
            intent_obj,
            f"Alright, I'll remind {whose} to {text} {every} at {clock(when)}, "
            f"startin' {starting_phrase(when, now)}{ending}.",
        )


class RemindAgainIntent(ReminderIntent):
    """remind me again in ... - reuses whatever last fired."""

    intent_type = "CustomReminderAgain"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        manager, refusal, _ = self._routed_manager(hass, intent_obj)
        if refusal:
            return self._say(intent_obj, refusal)
        if manager is None:
            return self._say(intent_obj, self._not_ready(hass))

        last = (manager.store.last_text or "").strip()
        if not last:
            return self._say(intent_obj, "Beg pardon, I don't have a reminder to repeat.")

        now = dt_util.now()
        when = when_from_slots(now, intent_obj.slots, self._default_time(manager))
        if when is None:
            return self._say(intent_obj, "Beg pardon, I couldn't work out when.")
        if when <= now:
            return self._say(intent_obj, "That time has already passed.")

        try:
            manager.schedule(last, when)
        except PastDue:
            return self._say(intent_obj, "That time has already passed.")
        whose = self._whose(hass, manager) or "you"
        return self._say(
            intent_obj,
            f"Alright, I'll remind {whose} about {last} at {clock(when)}{day_phrase(when, now)}.",
        )


# ---------------------------------------------------------------- skip


def _skip_empties_it(manager, reminder, scope: str, now: datetime) -> bool:
    """Would skipping take the last occurrence, leaving nothing behind?

    A series ends when its rule runs out, so skipping the final occurrence deletes the
    reminder outright. That is a cancellation wearing a skip's clothes, and it deserves
    the same question.
    """
    if not reminder.recurring:
        return True
    if scope == "next":
        return reminder.next_occurrence(reminder.due) is None
    occurrences = manager.store.occurrences_until(reminder, _period(now, "week")[1])
    if not occurrences:
        return False
    return reminder.next_occurrence(occurrences[-1]) is None


class SkipReminderIntent(ReminderIntent):
    """skip the next X / skip X this week."""

    intent_type = "CustomReminderSkip"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        manager, refusal, _ = self._routed_manager(hass, intent_obj)
        if refusal:
            return self._say(intent_obj, refusal)
        if manager is None:
            return self._say(intent_obj, self._not_ready(hass))

        slots = intent_obj.slots
        if _slot(slots, "use_last") is not None:
            needle = (manager.store.last_text or "").strip()
        else:
            needle = _text(slots, "reminder_text")
        if not needle:
            return self._say(intent_obj, "Beg pardon, I don't have a reminder to skip.")

        scope = _text(slots, "scope", "next") or "next"
        now = dt_util.now()

        matches = manager.store.find(needle)
        if not matches:
            return self._say(intent_obj, "Beg pardon, I couldn't find a reminder like that.")

        # Skipping a one-off, or the last of a series, removes it. That is a cancellation
        # however it was phrased, so it goes through the same question.
        one_offs = [r for r in matches if not r.recurring]
        emptied = [r for r in matches if _skip_empties_it(manager, r, scope, now)]
        if emptied:
            satellite = self._satellite(hass, manager, intent_obj)
            if satellite is not None and self._confirms(manager):
                first = describe(emptied[0], now)
                if len(matches) == 1 and one_offs:
                    question = (f"{first} only happens the once, so skippin' it cancels it. "
                                f"Are you sure?")
                elif len(matches) == 1:
                    question = (f"That's the last one of {first}, so skippin' it cancels "
                                f"the whole reminder. Are you sure?")
                else:
                    question = (f"That'd finish off {len(emptied)} of 'em, startin' with "
                                f"{first}. Are you sure?")
                # One-offs have nothing to skip, so for those the yes must CANCEL.
                as_skip = None if len(matches) == len(one_offs) else (needle, scope)
                hass.async_create_background_task(
                    _confirm_then_cancel(hass, manager, satellite, matches, question,
                                         skip=as_skip),
                    name="reminders_confirm_skip",
                )
                return self._say(intent_obj, "")
            if len(matches) == len(one_offs):
                # Nothing to skip on a one-off; the request means cancel.
                count = manager.cancel_reminders(matches)
                return self._say(intent_obj, _canceled_speech(count))

        result = manager.skip(needle, scope)

        # No confirmation, unlike canceling: this drops one occurrence and leaves the
        # series running, and the reply names what it dropped, so it announces itself.
        return self._say(intent_obj, _skip_speech(result, scope, now))


# ---------------------------------------------------------------- cancel


def _reask(names: str, heard: str | None) -> str:
    """The next ask, saying why there is one.

    Quoting the transcript is the difference between a person fixing the problem and a
    person repeating themselves into a speaker that will never agree - "I heard james"
    tells you instantly that the name never arrived, which asking again never does.
    """
    if not heard:
        return f"I didn't get that. Was it {names}?"
    return f"I heard {heard.strip().rstrip('.!?')}. Was it {names}?"


async def _ask_whose_once(hass, satellite: str, managers: list, question: str):
    """Ask once and return the list meant, or None if it was not answered.

    Returns (the list, what was heard). The transcript comes back either way, because the
    next question quotes it - "I heard 'james'" is the difference between a person fixing
    the problem and a person repeating themselves into a speaker that will never agree.
    """
    try:
        answer = await hass.services.async_call(
            "assist_satellite", "ask_question",
            {"entity_id": satellite,
             "question": question,
             "answers": [
                 {"id": m.entry.entry_id,
                  # Every shape a one-word answer arrives in. A bare name is the common
                  # one; the rest are what people actually say when prompted with two.
                  "sentences": [m.name, f"{m.name}'s", f"for {m.name}",
                                f"it's for {m.name}", f"it's {m.name}",
                                f"that's {m.name}", f"{m.name} please",
                                f"{m.name}'s list", f"put it on {m.name}'s list"]}
                 for m in managers
             ]},
            blocking=True, return_response=True,
        )
    except HomeAssistantError as err:
        # Silence, or a turn that never produced text. Not an answer, and not a refusal.
        _LOGGER.info("reminders: no answer to whose list (%s)", err)
        return None, None

    answer = answer or {}
    if chosen := next((m for m in managers if m.entry.entry_id == answer.get("id")), None):
        return chosen, answer.get("sentence")
    heard = (answer.get("sentence") or "").strip()
    if not heard:
        return None, None
    # No canned sentence matched, but something was said. The raw transcript is worth two
    # looks: the name inside a longer phrase, then a near miss on the name itself, which
    # is what "Jenn" or "Johns" arrives as.
    said = heard.casefold()
    named = [m for m in managers if m.name.casefold() in said]
    if len(named) != 1:
        named = [m for m in managers
                 if SequenceMatcher(None, m.name.casefold(), said.strip(" .!?")).ratio()
                 >= NAME_SIMILARITY]
    # Only when it points at exactly ONE list - "John or Jen?" repeated back is not an
    # answer, and guessing between two people is the one thing worse than asking again.
    if len(named) == 1:
        return named[0], heard
    _LOGGER.info("reminders: %r did not name one list", heard)
    return None, heard


_RE_DEFAULT_ANSWER = re.compile(r"^\s*(?:the\s+|use\s+the\s+|just\s+)?default\b", re.I)


async def _ask_free(hass, satellite: str, question: str) -> str | None:
    """Ask something open-ended and return what was said, or None for silence.

    No `answers` list: the reply is a time in whatever words the speaker chose, so there
    is nothing to enumerate and the raw transcript is the whole point.
    """
    try:
        answer = await hass.services.async_call(
            "assist_satellite", "ask_question",
            {"entity_id": satellite, "question": question},
            blocking=True, return_response=True,
        )
    except HomeAssistantError as err:
        _LOGGER.info("reminders: no answer to %r (%s)", question, err)
        return None
    said = (answer or {}).get("sentence")
    return said.strip() if isinstance(said, str) and said.strip() else None


async def _ask_when_then_schedule(hass, manager, satellite: str, text: str,
                                  question: str, default_time, whose: str) -> None:
    """Ask when a reminder should happen, then create it.

    This is what makes a reminder sayable in two breaths: name the task, then answer the
    time. The answer goes through the SAME parser the edit flow uses, so "at 6", "in
    twenty minutes" and "every Monday at 6" all work here without a second grammar.

    Silence is not an answer here either, so it is asked again rather than taken as
    agreement with the default - the same rule as the whose question. Saying "default"
    IS an answer and ends it at once. Only after the repeats does the default apply, and
    the reply then says the time out loud so a booked reminder never looks like an
    ignored one.
    """
    try:
        await asyncio.sleep(ASK_SETTLE)
        now = dt_util.now()
        due = rrule = None
        for attempt in range(ASK_REPEATS):
            said = await _ask_free(
                hass, satellite,
                question if attempt == 0 else f"Sorry - {question[0].lower()}{question[1:]}",
            )
            if said is None:
                continue                    # silence is not an answer - ask again
            if _RE_DEFAULT_ANSWER.match(said):
                break                       # "default" IS an answer, and it ends it
            _, due, rrule = _read_time(said, now, default_time)
            if due is not None:
                break
            _LOGGER.info("reminders: %r named no time", said)

        used_default = due is None
        if used_default:
            hour, minute = tuple(default_time)
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)

        reminder = manager.schedule(text, due, rrule)
        every = f" {reminder.every_base}" if reminder.recurring else ""
        day = "" if reminder.recurring else day_phrase(reminder.due, now)
        tail = f"{text}{every} at {clock(reminder.due)}{day}{reminder.every_bound}."
        # An unanswered question still creates the reminder, so the reply has to say the
        # time out loud - otherwise it is indistinguishable from having been ignored.
        opening = f"Right, the usual then. I'll remind {whose} to" if used_default \
            else f"Alright, I'll remind {whose} to"
        await _announce(hass, satellite, f"{opening} {tail}")
    except PastDue:
        await _announce(hass, satellite, "That time has already passed.")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("reminders: could not place the reminder after asking when")


async def _settle_whose(hass, satellite: str, managers: list, first: str):
    """Ask whose list until it is answered. None means it never was.

    Every question that has to know whose shares this: the opening words differ, the
    repeat rule does not. Silence is asked again, a transcript that named nobody is
    quoted back, and after ASK_REPEATS the CALLER decides what giving up costs - which
    is not the same for a reminder being made and a question being answered.
    """
    names = " or ".join(m.name for m in managers)
    question = f"{first} {names}?"
    for _ in range(ASK_REPEATS):
        chosen, heard = await _ask_whose_once(hass, satellite, managers, question)
        if chosen is not None:
            return chosen
        question = _reask(names, heard)
    _LOGGER.info("reminders: whose list went unanswered %d times", ASK_REPEATS)
    return None


async def _ask_whose_then_when(hass, handler, satellite: str, managers: list,
                               text: str) -> None:
    """Whose list, then when - a reminder that gave neither needs both asked.

    The two questions used to be mutually exclusive: a timeless reminder on a speaker two
    lists share hit the whose question and stopped there, so the one sentence needing the
    most help got the least. This order is not arbitrary - the second question's answer
    depends on the first, because the default hour it offers is the CHOSEN list's.
    """
    try:
        await asyncio.sleep(ASK_SETTLE)
        manager = await _settle_whose(hass, satellite, managers,
                                      "Who's this reminder for?")
        if manager is None:
            await _announce(hass, satellite,
                            "I still don't know whose that is, so I've not set it.")
            return
    except Exception:  # noqa: BLE001
        _LOGGER.exception("reminders: could not settle whose list")
        return
    # Its own task-shaped body, so the settle above is the only thing added: the when
    # question, its repeats and its fallback all behave exactly as they do on their own.
    await _ask_when_then_schedule(
        hass, manager, satellite, text,
        ("When should this reminder occur? "
         f"Default is {handler._default_time_phrase(manager)}."),
        handler._default_time(manager),
        handler._whose(hass, manager) or "you",
    )


async def _ask_whose_then_answer(hass, satellite: str, managers: list, answer) -> None:
    """Ask whose reminders a question is about, then speak the answer for that list.

    Same shape as the scheduling question and the same no-deadline repeat, but nothing
    is being created, so silence costs only a wrong-list answer rather than a lost
    reminder. After the repeats it answers for the default list - and every answer names
    whose it is once more than one list exists, so a fallback identifies itself.
    """
    try:
        await asyncio.sleep(ASK_SETTLE)
        chosen = await _settle_whose(hass, satellite, managers, "Whose reminders?")
        if chosen is None:
            await _announce(hass, satellite, "I still don't know whose you mean.")
            return
        await _announce(hass, satellite, answer(chosen))
    except Exception:  # noqa: BLE001
        _LOGGER.exception("reminders: could not answer after asking whose")


async def _ask_whose_then_schedule(hass, satellite: str, managers: list,
                                   text: str, due: datetime, rrule: str | None) -> None:
    """Ask whose list this is, then create it. Detached, like every other question.

    The question is asked until it is answered - there is no deadline on it anywhere,
    and an unanswered attempt is repeated rather than taken as an answer. After
    ASK_REPEATS spoken attempts it gives up and says so, rather than picking a list:
    with two people set up there is no such thing as a safe guess about whose reminder
    this is, and a reminder on the wrong person's list is worse than one not made.
    """
    try:
        await asyncio.sleep(ASK_SETTLE)
        chosen = await _settle_whose(hass, satellite, managers,
                                     "Who's this reminder for?")
        if chosen is None:
            await _announce(
                hass, satellite,
                "I still don't know whose that is, so I've not set it.",
            )
            return
        manager = chosen
        reminder = manager.schedule(text, due, rrule)
        now = dt_util.now()
        every = f" {reminder.every_base}" if reminder.recurring else ""
        when = clock(reminder.due)
        day = "" if reminder.recurring else day_phrase(reminder.due, now)
        tail = f"{text}{every} at {when}{day}{reminder.every_bound}."
        await _announce(hass, satellite, f"Alright, I'll remind {manager.name} to {tail}")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("reminders: could not place the reminder after asking")


async def _ask_then_edit(hass, manager, satellite: str, reminder_id: str,
                         default_time: tuple[int, int]) -> None:
    """Ask what the reminder should become, then apply it.

    Detached for the same reason the cancel confirmation is: ask_question opens a new
    conversation on the satellite, which will not start while the calling turn is open.
    The id is carried rather than the reminder itself - the wait is long enough for it to
    have fired and advanced, or gone.
    """
    try:
        await asyncio.sleep(ASK_SETTLE)
        try:
            answer = await hass.services.async_call(
                "assist_satellite", "ask_question",
                {"entity_id": satellite,
                 # The question teaches the three openings, because the answer is free
                 # speech and nothing else tells the user they exist.
                 "question": "What should I change? Say change the reminder to, "
                             "change the timing to, or replace with."},
                blocking=True, return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.info("reminders: no answer to the edit (%s), nothing changed", err)
            await _announce(hass, satellite, "Alright, leavin' it be.")
            return

        said = (answer or {}).get("sentence") or ""
        reminder = manager.store.reminders.get(reminder_id)
        if reminder is None:
            await _announce(hass, satellite, "Beg pardon, that one's gone now.")
            return

        now = dt_util.now()
        text, due, rrule = parse_edit_answer(said, now, default_time)
        if text is None and due is None and rrule is None:
            await _announce(hass, satellite, "Beg pardon, I didn't catch that. Nothin' changed.")
            return
        try:
            manager.edit(reminder, text, due, rrule)
        except PastDue:
            await _announce(hass, satellite, "That time has already passed. Nothin' changed.")
            return
        except InvalidRule:
            await _announce(hass, satellite, "I can't repeat one that often. Nothin' changed.")
            return

        every = f" {reminder.every_base}" if reminder.recurring else ""
        await _announce(
            hass, satellite,
            f"Alright, {reminder.text}{every} at {clock(reminder.due)}"
            f"{'' if reminder.recurring else day_phrase(reminder.due, now)}"
            f"{reminder.every_bound}.",
        )
    except Exception:  # noqa: BLE001 - a detached task must not die silently
        _LOGGER.exception("reminders: edit follow-up failed, nothing changed")


async def _confirm_then_cancel(hass, manager, satellite: str, matches, question: str,
                               skip: tuple[str, str] | None = None) -> None:
    """Ask on the satellite, and cancel only on yes.

    Runs detached from the turn that started it, because ask_question opens a new
    conversation on that satellite and the satellite will not start one while the
    previous is still closing out. The caller has already answered with silence, so
    every outcome from here has to be spoken.
    """
    try:
        await asyncio.sleep(ASK_SETTLE)
        confirmed = False
        try:
            answer = await hass.services.async_call(
                "assist_satellite",
                "ask_question",
                {
                    "entity_id": satellite,
                    "question": question,
                    "answers": [
                        {"id": "yes", "sentences": YES_SENTENCES},
                        {"id": "no", "sentences": NO_SENTENCES},
                    ],
                },
                blocking=True,
                return_response=True,
            )
            confirmed = (answer or {}).get("id") == "yes"
        except HomeAssistantError as err:
            # Saying nothing RAISES here ("No answer from question") rather than coming
            # back with an empty id, so silence arrives as an exception and never
            # reaches the check above. The YAML version had the same branch and could
            # not reach it either - the script simply errored out and said nothing.
            # Every way of not answering is a no, so they all land in one place.
            _LOGGER.info("reminders: not confirmed (%s), nothing canceled", err)

        if not confirmed:
            await _announce(hass, satellite, "Alright, not canceled.")
            return

        if skip is not None:
            # A skip that would empty the series is destructive enough to confirm, but
            # it is still a SKIP - anything in the match that has occurrences left keeps
            # them, and only the exhausted ones go.
            needle, scope = skip
            result = manager.skip(needle, scope)
            await _announce(hass, satellite, _skip_speech(result, scope, dt_util.now()))
            return

        # Re-read by id: the question took a few seconds, in which a reminder could
        # have fired and advanced or gone.
        live = [r for r in matches if r.id in manager.store.reminders]
        count = manager.cancel_reminders(live)
        await _announce(hass, satellite, _canceled_speech(count))
    except Exception:  # noqa: BLE001 - a detached task must not die silently
        # Fails CLOSED: anything unexpected leaves every reminder in place.
        _LOGGER.exception("reminders: confirmation failed, nothing canceled")


async def _announce(hass, satellite: str, message: str) -> None:
    await hass.services.async_call(
        "assist_satellite",
        "announce",
        {"entity_id": satellite, "message": message, "preannounce": False},
        blocking=True,
    )


def _skip_speech(result: dict, scope: str, now: datetime) -> str:
    """What a skip says afterwards. A function so the detached confirmation can say it too."""
    if result["success"] == RESULT_OK:
        # "the rest of 'em stay put" is a lie when there was no rest - skipping the last
        # occurrence exhausts the rule and the reminder is gone.
        ended = result.get("ended") or 0
        if scope == "week":
            count = result["number"]
            what = ("the only one left this week" if count == 1
                    else f"{count} of em, through Saturday")
            tail = " That was the last of it, so the reminder's done." if ended else \
                   " The series stays put."
            return f"Alright, skipped {what}.{tail}"
        dropped = result["occurrences"][0]
        if isinstance(dropped, str):
            dropped = dt_util.parse_datetime(dropped)
        tail = " That was the last one, so the reminder's done." if ended else \
               " The rest of 'em stay put."
        return (f"Alright, skippin' the one at {clock(dropped)}"
                f"{day_phrase(dropped, now)}.{tail}")
    if result["success"] == RESULT_NONE_LEFT:
        nxt = result["next"]
        if isinstance(nxt, str):
            nxt = dt_util.parse_datetime(nxt)
        return (f"There's nothin' left for that one this week. "
                f"Next one's at {clock(nxt)}{day_phrase(nxt, now)}.")
    if result["success"] == RESULT_NOT_RECURRING:
        return "That one only happens the once, so there's nothin' to skip."
    return "Beg pardon, I couldn't find a repeatin' reminder like that."


def _canceled_speech(count: int) -> str:
    if count == 0:
        return "Beg pardon, I couldn't find that one after all."
    if count == 1:
        return "Alright, canceled it."
    return f"Alright, canceled {count} of them."


class CancelReminderIntent(ReminderIntent):
    """cancel my X reminder / cancel my last reminder."""

    intent_type = "CustomReminderCancel"
    not_found = "Beg pardon, I don't have a reminder about that."

    def _matches(self, manager, intent_obj, loose: bool) -> list | None:
        slots = intent_obj.slots
        if _slot(slots, "use_last") is not None:
            needle = (manager.store.last_text or "").strip()
        else:
            needle = _text(slots, "reminder_text")
        if not needle:
            return None
        return manager.store.find_loose(needle) if loose else manager.store.find(needle)

    def _question(self, matches, now) -> str:
        first = describe(matches[0], now)
        if len(matches) == 1:
            if matches[0].recurring:
                return (
                    f"That one repeats. Are you sure you want me to cancel the recurring "
                    f"reminder to {first}? That'll stop every one of 'em, not just the next."
                )
            return f"Are you sure you want me to cancel the reminder to {first}?"
        tail = ", and one of 'em repeats" if any(r.recurring for r in matches) else ""
        return (
            f"Are you sure you want me to cancel {len(matches)} reminders, "
            f"startin with {first}{tail}?"
        )

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        manager, refusal, _ = self._routed_manager(hass, intent_obj)
        if refusal:
            return self._say(intent_obj, refusal)
        if manager is None:
            return self._say(intent_obj, self._not_ready(hass))

        # Whether a guess is allowed depends on whether it will be read back. A
        # confirmation names the match and waits for a yes, so a near-miss there is
        # recoverable and worth attempting - a recognizer that hears "Ozzy" as "Aussie"
        # otherwise leaves the user with no way to cancel by voice at all. With NOTHING
        # reading it back - typed, or confirmation switched off - only an exact match may
        # be acted on. The guess and the confirmation stand or fall together.
        will_confirm = self._confirms(manager)
        satellite = self._satellite(hass, manager, intent_obj)
        matches = self._matches(manager, intent_obj, loose=satellite is not None and will_confirm)
        if not matches:
            return self._say(intent_obj, self.not_found)

        now = dt_util.now()
        if satellite is None or not will_confirm:
            # Typed Assist, or a speaker that cannot hold a conversation. There is
            # nowhere to put a question, so cancel and answer in text.
            return self._say(intent_obj, _canceled_speech(manager.cancel_reminders(matches)))

        hass.async_create_background_task(
            _confirm_then_cancel(hass, manager, satellite, matches, self._question(matches, now)),
            name="reminders_confirm_cancel",
        )
        # Silence: the detached task above owns everything the user will hear.
        return self._say(intent_obj, "")


class CancelReminderAtIntent(CancelReminderIntent):
    """cancel my reminder at 8:15 tomorrow."""

    intent_type = "CustomReminderCancelAt"
    not_found = "Beg pardon, I don't have a reminder at that time."

    def _matches(self, manager, intent_obj, loose: bool) -> list | None:
        when = when_at_slots(dt_util.now(), intent_obj.slots)
        if when is None:
            return None
        # By time it takes the ONE row it means. The YAML version deleted over a
        # +/-2 minute window, so two reminders in the same minute both died.
        found = manager.find_at(when)
        return found[:1]


# ---------------------------------------------------------------- list, fallback


class ListRemindersIntent(ReminderIntent):
    """what are my upcoming reminders."""

    intent_type = "CustomReminderList"
    LIMIT = 5

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        return self._respond(intent_obj)

    def _answer(self, hass, manager, intent_obj) -> str:
        upcoming = manager.store.upcoming()
        who = self._whose(hass, manager)
        if not upcoming:
            return f"{who} ain't got any reminders set." if who else "You ain't got any reminders set."
        now = dt_util.now()
        parts = [describe(r, now) for r in upcoming[: self.LIMIT]]
        count = len(upcoming)
        head = f"{count} reminder{'' if count == 1 else 's'}"
        if who:
            head = f"{who} has {head}"
        if count > self.LIMIT:
            head += ", soonest five"
        return f"{head}: {'; '.join(parts)}."


def _period(now: datetime, kind: str) -> tuple[datetime, datetime] | None:
    """A named period as a (start, end) pair.

    "this week" ends on Saturday, matching what skip already means by it - two phrasings
    of the same word must not disagree about which days they cover.
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of = lambda d: d.replace(hour=23, minute=59, second=59, microsecond=0)
    if kind == "today":
        return now, end_of(day_start)
    if kind == "tomorrow":
        nxt = day_start + timedelta(days=1)
        return nxt, end_of(nxt)
    if kind == "week":
        return now, end_of(day_start + timedelta(days=(5 - now.weekday()) % 7))
    if kind == "nextweek":
        start = day_start + timedelta(days=(6 - now.weekday()) % 7 or 7)
        return start, end_of(start + timedelta(days=6))
    if kind == "month":
        return now, end_of(_add_months(day_start.replace(day=1), 1) - timedelta(days=1))
    if kind == "nextmonth":
        start = _add_months(day_start.replace(day=1), 1)
        return start, end_of(_add_months(start, 1) - timedelta(days=1))
    return None


class FindRemindersIntent(ReminderIntent):
    """when is my X reminder / what have I got next week."""

    intent_type = "CustomReminderFind"
    LIMIT = 5

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        return self._respond(intent_obj)

    def _answer(self, hass, manager, intent_obj) -> str:
        slots = intent_obj.slots
        now = dt_util.now()
        needle = _text(slots, "reminder_text") or None
        window = None
        label = ""
        if kind := _text(slots, "when_kind"):
            window = _period(now, kind)
            label = {"today": " today", "tomorrow": " tomorrow", "week": " this week",
                     "nextweek": " next week", "month": " this month",
                     "nextmonth": " next month"}.get(kind, "")
        elif (weekday := _int(slots, "weekday")) is not None:
            start = (now + timedelta(days=(weekday - now.weekday()) % 7)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            window = (max(start, now), start.replace(hour=23, minute=59, second=59))
            label = f" on {DAY_NAMES[weekday]}"

        start, end = window if window else (None, None)
        found = manager.search(needle, start, end)
        who = self._whose(hass, manager)
        if not found:
            empty = f"{who}'s got nothin'" if who else "You've got nothin'"
            return f"{empty} about {needle}." if needle else f"{empty}{label}."

        # A label that already names one day ("today", "on Thursday") makes repeating the
        # day on every row noise; "this week" does not, so those rows keep it.
        names_the_day = label in (" today", " tomorrow") or label.startswith(" on ")
        parts = []
        for reminder, occurrences in found[: self.LIMIT]:
            # For a range, name the occurrence INSIDE it. `due` is the next occurrence
            # overall, which for a series can be nowhere near what was asked about.
            when = occurrences[0] if occurrences else reminder.due
            extra = ""
            if occurrences and len(occurrences) > 1:
                extra = f" and {len(occurrences) - 1} more"
            day = "" if names_the_day else day_phrase(when, now)
            parts.append(f"{reminder.text} at {clock(when)}{day}{extra}")
        count = len(found)
        head = f"{count} reminder{'' if count == 1 else 's'}{label}"
        if who:
            head = f"{who} has {head}"
        if count > self.LIMIT:
            head += ", soonest five"
        return f"{head}: {'; '.join(parts)}."


class EditReminderIntent(ReminderIntent):
    """move my X reminder to 6 pm / make it repeat every Thursday / stop it repeating."""

    intent_type = "CustomReminderEdit"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        manager, refusal, _ = self._routed_manager(hass, intent_obj)
        if refusal:
            return self._say(intent_obj, refusal)
        if manager is None:
            return self._say(intent_obj, self._not_ready(hass))

        slots = intent_obj.slots
        needle = _text(slots, "reminder_text")
        if not needle:
            return self._say(intent_obj, "Beg pardon, which reminder?")
        matches = manager.store.find(needle)
        if not matches:
            return self._say(intent_obj, "Beg pardon, I don't have a reminder about that.")
        if len(matches) > 1:
            # Editing the wrong one is quietly wrong, and nothing reads it back the way
            # canceling does. Ask rather than guess.
            return self._say(
                intent_obj,
                f"You've got {len(matches)} reminders like that - "
                f"{'; '.join(m.text for m in matches[:3])}. "
                f"You'll have to be clearer about which one.",
            )
        reminder = matches[0]
        now = dt_util.now()
        # A change to the PATTERN is not a change to the hour. Where the sentence names
        # no clock time, the reminder's OWN time is the fallback rather than the
        # configured default: "make it repeat every Tuesday" said nothing about moving it
        # to 8:15. A spoken hour still wins, and so does a part of the day, because both
        # are read before the fallback is reached.
        default = self._default_time(manager)
        keep = Times((reminder.due.hour, reminder.due.minute), default.parts)

        if _slot(slots, "edit_ask") is not None:
            # "edit my X reminder" names the reminder but not the change, so ask for it.
            satellite = self._satellite(hass, manager, intent_obj)
            if satellite is None:
                # Typed, so there is nowhere to ask. Say what it is and how to say it in
                # one go, rather than opening a conversation that cannot be finished.
                #
                # The example names the list when there is more than one: "move MY ...
                # reminder" is exactly the phrasing that would come back asking whose,
                # and a worked example has to be one that works.
                name = self._whose(hass, manager)
                whose = f"{name}'s" if name else "my"
                return self._say(
                    intent_obj,
                    f"{reminder.text} is at {clock(reminder.due)}"
                    f"{day_phrase(reminder.due, now)}"
                    f"{(' - ' + reminder.every) if reminder.recurring else ''}. "
                    f"Say it all at once, like 'move {whose} "
                    f"{reminder.text} reminder to 6 pm'.",
                )
            hass.async_create_background_task(
                _ask_then_edit(hass, manager, satellite, reminder.id, keep),
                name="reminders_ask_edit",
            )
            return self._say(intent_obj, "")

        try:
            if _slot(slots, "stop_repeating") is not None:
                if not reminder.recurring:
                    return self._say(intent_obj, f"{reminder.text} doesn't repeat anyway.")
                manager.edit(reminder, rrule="")
                return self._say(
                    intent_obj,
                    f"Alright, {reminder.text} won't repeat. It's still set for "
                    f"{clock(reminder.due)}{day_phrase(reminder.due, now)}.",
                )

            if _slot(slots, "recur") is not None or _slot(slots, "recur_kind") is not None:
                found = recurrence_from_slots(now, slots, "", keep)
                if found is None:
                    return self._say(intent_obj, "Beg pardon, I couldn't work out the new pattern.")
                when, rrule, every = found
                manager.edit(reminder, due=when, rrule=rrule)
                return self._say(
                    intent_obj,
                    f"Alright, {reminder.text} now repeats {every} at {clock(when)}, "
                    f"startin' {starting_phrase(when, now)}.",
                )

            when = when_from_slots(now, slots, keep)
            if when is None:
                return self._say(intent_obj, "Beg pardon, I couldn't work out when.")
            was_recurring = reminder.recurring
            manager.edit(reminder, due=when)
            tail = " Every one of 'em moves." if was_recurring else ""
            return self._say(
                intent_obj,
                f"Alright, {reminder.text} is now at {clock(when)}"
                f"{day_phrase(when, now)}.{tail}",
            )
        except PastDue:
            return self._say(intent_obj, "That time has already passed.")
        except InvalidRule:
            return self._say(intent_obj, "Beg pardon, I can't repeat one that often.")


class NextReminderIntent(ReminderIntent):
    """what is my next reminder."""

    intent_type = "CustomReminderNext"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        return self._respond(intent_obj)

    def _answer(self, hass, manager, intent_obj) -> str:
        upcoming = manager.store.upcoming()
        who = self._whose(hass, manager)
        if not upcoming:
            return f"{who} ain't got any reminders set." if who else "You ain't got any reminders set."
        whose = f"{who}'s next reminder" if who else "Your next reminder"
        return f"{whose} is {describe(upcoming[0], dt_util.now())}."


class CountRemindersIntent(ReminderIntent):
    """how many reminders do I have."""

    intent_type = "CustomReminderCount"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        return self._respond(intent_obj)

    def _answer(self, hass, manager, intent_obj) -> str:
        # The same number todo.reminders shows as its state, counted from the store so
        # that renaming the entity cannot break the answer.
        count = len(manager.store.reminders)
        who = self._whose(hass, manager)
        subject = f"{who} currently has" if who else "You currently have"
        if count == 0:
            return f"{subject} no reminders."
        return f"{subject} {count} reminder{'' if count == 1 else 's'}."


class ReminderFallbackIntent(ReminderIntent):
    """Anything reminder-shaped the grammar did not understand.

    Without this it falls through to the conversation agent, which answers as though it
    scheduled something and does not. Refusing is the honest answer.
    """

    intent_type = "CustomReminderFallback"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        manager, refusal, ambiguous = self._routed_manager(hass, intent_obj)
        if refusal:
            return self._say(intent_obj, refusal)

        text = _task_only(_text(intent_obj.slots, "fallback_rest"))
        if not text:
            return self._say(intent_obj, "No comprende, partner.")

        if ambiguous:
            # Neither whose nor when was given. Both get asked, in that order - the
            # sentence that supplied the least is the one that should be helped most,
            # and it used to be the only one helped not at all.
            satellite = self._any_satellite(hass, _lists(hass), intent_obj)
            if satellite is not None:
                hass.async_create_background_task(
                    _ask_whose_then_when(hass, self, satellite, _lists(hass), text),
                    name="reminders_ask_whose_when",
                )
                return self._say(intent_obj, "")
        if manager is None:
            return self._say(intent_obj, self._not_ready(hass))

        question = ("When should this reminder occur? "
                    f"Default is {self._default_time_phrase(manager)}.")
        satellite = self._satellite(hass, manager, intent_obj)
        if satellite is None:
            # Nothing to ask ON, so there is no turn to answer into. The question is
            # still the useful reply: it names exactly what the sentence was missing.
            return self._say(intent_obj, question)
        hass.async_create_background_task(
            _ask_when_then_schedule(
                hass, manager, satellite, text, question,
                self._default_time(manager), self._whose(hass, manager) or "you",
            ),
            name="reminders_ask_when",
        )
        return self._say(intent_obj, "")


# ---------------------------------------------------------------- registration


HANDLERS = [
    SetReminderIntent,
    RecurringReminderIntent,
    RemindAgainIntent,
    SkipReminderIntent,
    CancelReminderIntent,
    CancelReminderAtIntent,
    ListRemindersIntent,
    NextReminderIntent,
    CountRemindersIntent,
    FindRemindersIntent,
    EditReminderIntent,
    ReminderFallbackIntent,
]


REGISTERED = f"{DOMAIN}_intents_registered"


def async_register_intents(hass: HomeAssistant) -> None:
    """Register every handler, once.

    Called from both async_setup and async_setup_entry, because either can be the first
    to run. Re-registering is harmless but logs "Intent X is being overwritten" at
    WARNING for each handler on every start, and a warning that is always there is a
    warning nobody reads.
    """
    if hass.data.get(REGISTERED):
        return
    hass.data[REGISTERED] = True
    for handler in HANDLERS:
        intent.async_register(hass, handler())
