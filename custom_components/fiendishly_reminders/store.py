"""Persistent store for reminders, and the recurrence arithmetic.

The store is the source of truth, not the to-do list. TodoItem has fields for uid,
summary, status, due, description and completed - and nothing at all for recurrence -
so a repeating reminder cannot be represented as a to-do item. The to-do entity is a
projection of what is kept here.

`exdates` is the other thing a to-do item cannot hold: single occurrences dropped by
hand. Skipping one occurrence of a series is a real operation, and it cannot be
expressed by moving the due date, because the series has to carry on unchanged around
the hole. This is what iCalendar's EXDATE is for.
"""

from __future__ import annotations

import logging
import re
import uuid
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from dateutil.rrule import rrulestr

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY, STORAGE_VERSION, VALID_FREQS

_LOGGER = logging.getLogger(__name__)

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Words that carry no reference on their own, so sharing one is not evidence of a match.
_STOPWORDS = {
    "a", "an", "the", "my", "me", "to", "for", "of", "and", "at", "on", "in", "it",
    "this", "that", "is", "reminder", "reminders", "about",
}


# How far ahead the best loose candidate must be before it counts as the one meant,
# and how similar it must be in its own right once anything else is competing with it.
AMBIGUOUS_MARGIN = 0.15
CONFIDENT_SIMILARITY = 0.7


def _tokens(text: str) -> list[str]:
    """Lower-case words, punctuation discarded. "walk, Ozzy." -> ["walk", "ozzy"]."""
    return [t for t in re.split(r"[^0-9a-z]+", (text or "").casefold()) if t]


def _content_tokens(text: str) -> list[str]:
    return [t for t in _tokens(text) if t not in _STOPWORDS]
_BYDAY_TO_INDEX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class InvalidRule(ValueError):
    """The recurrence rule is not one we will store."""


class PastDue(ValueError):
    """The requested time has already gone by.

    Lives here beside InvalidRule rather than in __init__, so the intent handlers can
    catch it without importing the package that imports them.
    """


def normalize_rrule(value: str | None) -> str | None:
    """Return a bare, validated RRULE, or None.

    Accepts the rule with or without an "RRULE:" prefix because people write it both
    ways. Rejects anything finer than daily: dateutil parses FREQ=HOURLY quite happily,
    which is exactly how a value Home Assistant refuses to read back got written to a
    calendar and made every subsequent read of it raise.
    """
    if value is None:
        return None
    rule = value.strip()
    if not rule:
        return None
    if rule.upper().startswith("RRULE:"):
        rule = rule[6:].strip()

    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = (parts.get("FREQ") or "").upper()
    if not freq:
        raise InvalidRule(f"rrule has no FREQ: {value!r}")
    if freq not in VALID_FREQS:
        raise InvalidRule(
            f"unsupported frequency {freq!r} - use one of {', '.join(sorted(VALID_FREQS))}"
        )
    rule = _local_until(rule)
    try:
        rrulestr(f"RRULE:{rule}", dtstart=datetime(2020, 1, 1))
    except (ValueError, TypeError) as err:
        raise InvalidRule(f"invalid rrule {value!r}: {err}") from err
    return rule


def _local_until(rule: str) -> str:
    """Rewrite UNTIL into NAIVE LOCAL time, which is the only form that works here.

    Occurrences are expanded against a naive local dtstart so that 9am stays 9am across
    a DST change, and dateutil refuses to mix a naive dtstart with the UTC "...Z" UNTIL
    that RFC5545 asks for - "RRULE UNTIL values must be specified in UTC when DTSTART is
    timezone-aware", raised the other way round. Rather than make dtstart aware and lose
    the DST behavior, the bound is converted to match it.

    A date-only UNTIL means the whole of that day, so it becomes 23:59:59 rather than
    midnight - otherwise "until October 18" silently excludes the 18th.
    """
    match = re.search(r"(UNTIL=)([0-9]{8})(T[0-9]{6})?(Z?)", rule, re.I)
    if not match:
        return rule
    _, date_part, time_part, zulu = match.groups()
    if not time_part:
        stamp = datetime.strptime(date_part, "%Y%m%d").replace(
            hour=23, minute=59, second=59
        )
    else:
        stamp = datetime.strptime(date_part + time_part, "%Y%m%dT%H%M%S")
        if zulu:
            stamp = dt_util.as_local(stamp.replace(tzinfo=timezone.utc)).replace(tzinfo=None)
    return rule[: match.start()] + "UNTIL=" + stamp.strftime("%Y%m%dT%H%M%S") + rule[match.end():]


def _ordinal(n: int) -> str:
    if n in (1, 21, 31):
        return "st"
    if n in (2, 22):
        return "nd"
    if n in (3, 23):
        return "rd"
    return "th"


def _bound_prose(parts: dict) -> str:
    """", until October 18" or ", 6 times" - whatever ends the series, if anything.

    Part of the prose rather than a separate field on purpose: the to-do row, the list
    intent and the dashboard card all render `every`, so saying it once here is what
    makes a bounded series announce itself everywhere instead of in one place.
    """
    if count := parts.get("COUNT"):
        n = int(count)
        return f", {n} time" + ("" if n == 1 else "s")
    if until := parts.get("UNTIL"):
        stamp = until.rstrip("Z")
        try:
            when = datetime.strptime(stamp[:15], "%Y%m%dT%H%M%S") if "T" in stamp \
                else datetime.strptime(stamp[:8], "%Y%m%d")
        except ValueError:
            return ""
        return f", until {_MONTH_NAMES[when.month - 1]} {when.day}"
    return ""


def describe_rrule(rule: str | None, with_bound: bool = True) -> str:
    """Plain prose for a rule, for the to-do item and the dashboard.

    Built once here rather than re-derived in Jinja by every card that wants it.
    """
    if not rule:
        return ""
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = (parts.get("FREQ") or "").upper()
    byday = [d for d in (parts.get("BYDAY") or "").split(",") if d]

    if freq == "DAILY":
        base = "every day"
    elif freq == "WEEKLY":
        if sorted(byday) == ["FR", "MO", "TH", "TU", "WE"]:
            base = "every weekday"
        elif sorted(byday) == ["SA", "SU"]:
            base = "every weekend day"
        elif len(byday) == 1:
            base = f"every {_DAY_NAMES[_BYDAY_TO_INDEX[byday[0]]]}"
        elif byday:
            base = "every " + ", ".join(_DAY_NAMES[_BYDAY_TO_INDEX[d]] for d in byday)
        else:
            base = "every week"
    elif freq == "MONTHLY":
        if day := parts.get("BYMONTHDAY"):
            n = int(day)
            base = f"on the {n}{_ordinal(n)} of every month"
        else:
            base = "every month"
    else:
        base = None
    if base is not None:
        return base + (_bound_prose(parts) if with_bound else "")
    if freq == "YEARLY":
        base = "every year"
    else:
        return ""
    return base + (_bound_prose(parts) if with_bound else "")


@dataclass
class Reminder:
    """One reminder. `due` is the NEXT occurrence, `dtstart` anchors the series."""

    id: str
    text: str
    due: datetime
    dtstart: datetime
    rrule: str | None = None
    exdates: list[datetime] = field(default_factory=list)
    created: datetime | None = None
    last_fired: datetime | None = None

    @property
    def recurring(self) -> bool:
        return self.rrule is not None

    @property
    def every(self) -> str:
        """Full prose, bound included: "every day, until September 8"."""
        return describe_rrule(self.rrule)

    @property
    def _rule_parts(self) -> dict:
        if not self.rrule:
            return {}
        return dict(p.split("=", 1) for p in self.rrule.split(";") if "=" in p)

    @property
    def count(self) -> int | None:
        """How many occurrences the series has in total, if it was given a COUNT.

        Exposed as a field of its own because the alternative is asking every consumer to
        parse an RRULE string, which is the very thing `every` exists to avoid. It is the
        total the series was created with, not the number left.
        """
        value = self._rule_parts.get("COUNT")
        return int(value) if value and value.isdigit() else None

    @property
    def until(self) -> datetime | None:
        """The date the series stops, if it was given an UNTIL."""
        value = self._rule_parts.get("UNTIL")
        if not value:
            return None
        stamp = value.rstrip("Z")
        try:
            when = (
                datetime.strptime(stamp[:15], "%Y%m%dT%H%M%S")
                if "T" in stamp
                else datetime.strptime(stamp[:8], "%Y%m%d")
            )
        except ValueError:
            return None
        return when.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    @property
    def is_last(self) -> bool:
        """True when the next occurrence is the final one.

        Skipping it exhausts the rule and removes the reminder, so a caller that offers
        "skip" needs to know it is really offering "delete". Computed here because it
        takes expanding the rule, which nothing outside this module should be doing.
        """
        return self.recurring and self.next_occurrence(self.due) is None

    @property
    def every_base(self) -> str:
        """Just the pattern: "every day". """
        return describe_rrule(self.rrule, with_bound=False)

    @property
    def every_bound(self) -> str:
        """Just the ending: ", until September 8".

        Split out because a sentence wants the two either side of the time - "every day
        at 8:15 AM, until September 8" - while a card, which shows the time in its own
        column, wants them together.
        """
        full, base = self.every, self.every_base
        return full[len(base):] if full.startswith(base) else ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "due": self.due.isoformat(),
            "dtstart": self.dtstart.isoformat(),
            "rrule": self.rrule,
            "exdates": [d.isoformat() for d in self.exdates],
            "created": self.created.isoformat() if self.created else None,
            "last_fired": self.last_fired.isoformat() if self.last_fired else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Reminder:
        def _dt(value):
            return dt_util.parse_datetime(value) if value else None

        return cls(
            id=data["id"],
            text=data["text"],
            due=_dt(data["due"]),
            dtstart=_dt(data.get("dtstart")) or _dt(data["due"]),
            rrule=data.get("rrule"),
            exdates=[_dt(d) for d in data.get("exdates", []) if d],
            created=_dt(data.get("created")),
            last_fired=_dt(data.get("last_fired")),
        )

    def next_occurrence(self, after: datetime) -> datetime | None:
        """First occurrence strictly after `after`, skipping exdates.

        Expanded on naive local times and re-stamped with the local zone afterwards, so
        that a 9am reminder stays at 9am across a DST change rather than sliding by an
        hour. Anchored on dtstart rather than on the current due date so the pattern
        cannot drift as the reminder advances.
        """
        if not self.rrule:
            return None
        tz = dt_util.DEFAULT_TIME_ZONE
        naive_start = dt_util.as_local(self.dtstart).replace(tzinfo=None)
        cursor = dt_util.as_local(after).replace(tzinfo=None)
        skip = {dt_util.as_local(d).replace(tzinfo=None) for d in self.exdates}

        rule = rrulestr(f"RRULE:{self.rrule}", dtstart=naive_start)
        # A guard, not a limit: without it a rule whose every occurrence is excluded
        # would spin forever.
        for _ in range(500):
            cand = rule.after(cursor, inc=False)
            if cand is None:
                return None
            if cand not in skip:
                return cand.replace(tzinfo=tz)
            cursor = cand
        _LOGGER.warning("reminders: gave up finding an occurrence for %r", self.text)
        return None


class ReminderStore:
    """Loads, holds and saves the reminders."""

    def __init__(self, hass: HomeAssistant, entry_id: str | None = None) -> None:
        # One file per config entry, so two people's lists cannot overwrite each other.
        key = f"{STORAGE_KEY}.{entry_id}" if entry_id else STORAGE_KEY
        self._store: Store = Store(hass, STORAGE_VERSION, key)
        # The single-instance file this used to be. Read once, then removed - leaving it
        # would let a SECOND instance adopt the same reminders on its first load.
        self._legacy: Store | None = (
            Store(hass, STORAGE_VERSION, STORAGE_KEY) if entry_id else None
        )
        self.reminders: dict[str, Reminder] = {}
        # The last reminder that actually FIRED, which outlives the reminder itself:
        # "remind me again" and "cancel my last reminder" both need the text after the
        # row has been deleted or advanced. The YAML version kept this in
        # input_text.last_reminder; owning it here is what lets that helper retire.
        self.last_text: str | None = None
        self.last_fired: datetime | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load()
        migrated = False
        if data is None and self._legacy is not None:
            if legacy := await self._legacy.async_load():
                _LOGGER.info("reminders: adopting the single-instance store")
                data, migrated = legacy, True
        data = data or {}
        self.last_text = data.get("last_text")
        if raw_at := data.get("last_fired"):
            self.last_fired = dt_util.parse_datetime(raw_at)
        for raw in data.get("reminders", []):
            try:
                reminder = Reminder.from_dict(raw)
            except Exception:  # noqa: BLE001 - one bad row must not lose the rest
                _LOGGER.exception("reminders: could not load %s", raw)
                continue
            self.reminders[reminder.id] = reminder
        _LOGGER.debug("reminders: loaded %d", len(self.reminders))
        if migrated:
            # Write to the new key first, then drop the old file, so a crash between the
            # two leaves the reminders readable rather than nowhere.
            await self._store.async_save(self._data())
            await self._legacy.async_remove()
            _LOGGER.info("reminders: migrated %d to its own store", len(self.reminders))

    async def async_remove(self) -> None:
        """Delete this list's file. Called when its config entry is removed."""
        await self._store.async_remove()

    def _data(self) -> dict:
        return {
            "reminders": [r.to_dict() for r in self.reminders.values()],
            "last_text": self.last_text,
            "last_fired": self.last_fired.isoformat() if self.last_fired else None,
        }

    def record_fired(self, reminder: Reminder, when: datetime) -> None:
        """Remember what just fired, for "remind me again"."""
        self.last_text = reminder.text
        self.last_fired = when
        self.async_save()

    def async_save(self) -> None:
        """Queue a save. Delayed, so a burst of edits writes once."""
        self._store.async_delay_save(self._data, 5)

    def add(
        self,
        text: str,
        due: datetime,
        rrule: str | None = None,
    ) -> Reminder:
        rule = normalize_rrule(rrule)
        due = dt_util.as_local(due)
        reminder = Reminder(
            id=uuid.uuid4().hex,
            text=text,
            due=due,
            dtstart=due,
            rrule=rule,
            created=dt_util.now(),
        )
        self.reminders[reminder.id] = reminder
        self.async_save()
        return reminder

    def remove(self, reminder_id: str) -> Reminder | None:
        """Delete a reminder outright. For a series this takes every occurrence.

        That is deliberate and it is the only series-level delete here. Selecting
        reminders by TIME and then removing them by ID is what nearly destroyed every
        recurring reminder in the YAML version, when a nightly sweep of past events
        matched one aged occurrence and deleted the live series with it.
        """
        reminder = self.reminders.pop(reminder_id, None)
        if reminder:
            self.async_save()
        return reminder

    def skip_occurrence(self, reminder: Reminder, occurrence: datetime) -> bool:
        """Drop one occurrence and advance. The series carries on."""
        if not reminder.recurring:
            return False
        reminder.exdates.append(dt_util.as_local(occurrence))
        nxt = reminder.next_occurrence(occurrence)
        if nxt is None:
            self.remove(reminder.id)
            return True
        reminder.due = nxt
        self.async_save()
        return True

    def occurrences_until(self, reminder: Reminder, end: datetime) -> list[datetime]:
        """Every occurrence from `due` up to and including `end`."""
        if reminder.due > end:
            return []
        out = [reminder.due]
        if not reminder.recurring:
            return out
        cursor = reminder.due
        while len(out) < 200:
            nxt = reminder.next_occurrence(cursor)
            if nxt is None or nxt > end:
                break
            out.append(nxt)
            cursor = nxt
        return out

    def find(self, needle: str) -> list[Reminder]:
        """Reminders matching `needle`, soonest first. Precise: two tiers, no guessing.

        Tier 1 is the plain substring match. Tier 2 exists because the needle comes from
        SPEECH, and speech carries things the stored text does not: punctuation
        ("walk, Ozzy"), filler the recognizer inserted ("walk but with Ozzy"), and
        trailing qualifiers the speaker added ("walk Ozzy tomorrow morning"). All three
        break a substring match while naming the reminder unambiguously. So tier 2 asks
        the other question - is every word of the stored reminder present in what was
        said - which those three satisfy and an unrelated reminder does not.
        """
        needle = (needle or "").strip()
        if not needle:
            return sorted(self.reminders.values(), key=lambda r: r.due)
        # A needle that is nothing but filler is not a reference to anything, and as a
        # bare substring it matches most of the list - "the" finds "take the trash out".
        # Misrecognized speech is the way that gets here.
        if not any(len(t) >= 3 for t in _content_tokens(needle)):
            return []

        exact = [r for r in self.reminders.values() if needle.casefold() in r.text.casefold()]
        if exact:
            return sorted(exact, key=lambda r: r.due)

        spoken = set(_tokens(needle))
        contained = []
        for reminder in self.reminders.values():
            words = set(_tokens(reminder.text))
            # One short word in common is not a reference to a reminder.
            if words and words <= spoken and any(len(w) >= 3 for w in words):
                contained.append(reminder)
        return sorted(contained, key=lambda r: r.due)

    def find_loose(self, needle: str) -> list[Reminder]:
        """`find`, and failing that the closest thing by shared words.

        ONLY for callers that will read the match back and wait for a yes. A recognizer
        that hears "Ozzy" as "Aussie" leaves no characters in common to match on -
        difflib scores that pair 0.5, below an unrelated reminder on the same list - so
        the only thing left to go on is the words that did survive. Guessing from that
        is safe exactly and only when the guess is named out loud before anything
        happens to it, which is why this is not what the plain service calls.
        """
        if hits := self.find(needle):
            return hits

        spoken = {t for t in _content_tokens(needle) if len(t) >= 3}
        if not spoken:
            return []
        said = " ".join(_tokens(needle))
        scored: list[tuple[float, Reminder]] = []
        for reminder in self.reminders.values():
            shared = spoken & {t for t in _content_tokens(reminder.text) if len(t) >= 3}
            if not shared:
                continue
            similarity = SequenceMatcher(None, said, " ".join(_tokens(reminder.text))).ratio()
            scored.append((len(shared) + similarity, reminder))
        if not scored:
            return []

        # AT MOST ONE, ever. Returning every joint-best candidate is how "cancel my walk
        # Roofus reminder" came to offer - and on a single yes destroy - both "walk
        # Rufus" and "walk Ozzy", which tied on the word "walk".
        if len(scored) == 1:
            return [scored[0][1]]

        # With rivals, the shared word no longer distinguishes them and something has to.
        # Character similarity is all there is, and it is only trustworthy when it is
        # HIGH: it is actively misleading in the middle, where "walk aussie" scores
        # closer to "walk rufus" (0.67) than to the "walk Ozzy" actually meant (0.50),
        # because a mishearing is a phonetic neighbour and not a spelling one. So a rival
        # is resolved only by a near-certain match that is also clearly ahead; anything
        # less is two plausible readings, and picking one of those is not a guess worth
        # confirming. Saying so is the honest answer.
        scored.sort(key=lambda pair: pair[0], reverse=True)
        best, runner_up = scored[0], scored[1]
        if best[0] - 1 < CONFIDENT_SIMILARITY or best[0] - runner_up[0] < AMBIGUOUS_MARGIN:
            return []
        return [best[1]]

    def upcoming(self) -> list[Reminder]:
        return sorted(self.reminders.values(), key=lambda r: r.due)
