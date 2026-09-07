"""Reminders - scheduling and delivery.

The whole reason this exists as an integration rather than as YAML is one line:
async_track_point_in_time. Being able to schedule an arbitrary future callback removes
the near-term/calendar split, the fixed timer slots, the "no slots free" refusal and the
nightly cleanup of fired events, all of which existed only to work around not having it.

Two behaviors follow from owning the schedule that a to-do-list-plus-polling design
cannot have: reminders fire at the second rather than at the next poll, and a reminder
that came due while Home Assistant was down is delivered late instead of vanishing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import (
    CoreState,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CHIME_FILE,
    RESULT_BAD_RULE,
    RESULT_BAD_TIME,
    RESULT_EMPTY,
    RESULT_NO_CHANGE,
    RESULT_NO_LIST,
    RESULT_NO_MATCH,
    RESULT_NO_WHOSE,
    RESULT_NO_TARGET,
    RESULT_NONE_LEFT,
    RESULT_NOT_RECURRING,
    RESULT_OK,
    RESULT_PAST,
    CHIME_CHOICES,
    CHIME_CUSTOM,
    CHIME_HA,
    CHIME_NONE,
    CHIME_DIR,
    CHIME_DIR_URL,
    CHIME_URL,
    CONF_ANNOUNCE_TARGETS,
    CONF_CHIME,
    CONF_CHIME_ON,
    CONF_NAME,
    CONF_NOTIFY_SERVICE,
    CONF_PRESENCE_ENTITY,
    CONF_QUIET_END,
    CONF_QUIET_START,
    DEFAULT_CHIME,
    DEFAULT_CHIME_ON,
    DOMAIN,
    PLATFORMS,
    STARTUP_GRACE,
)
from .intents import async_register_intents
from .store import InvalidRule, PastDue, Reminder, ReminderStore, normalize_rrule

_LOGGER = logging.getLogger(__name__)

CHIME_SERVED = f"{DOMAIN}_chime_served"

# Allows a bare `fiendishly_reminders:` in configuration.yaml. That key does nothing
# except make Home Assistant set this component up during BOOTSTRAP, which is the only
# way to get the voice intents registered before the config entry loads - the entry
# waits behind zha, august and nest, and a reminder sentence in that window would
# otherwise be answered "Unknown intent".
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

# `due` accepts a raw string as well as a datetime so that an unreadable one reaches the
# handler and comes back as success: false, rather than being rejected by schema
# validation - which aborts the caller before it can look at any response.
# `target` names whose list to act on - a name like "Jen", or an entry id. Absent means
# the default list, which is the only list when only one is configured.
SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("target"): cv.string,
        vol.Required("text"): cv.string,
        vol.Required("due"): vol.Any(cv.datetime, cv.string),
        vol.Optional("rrule"): vol.Any(cv.string, None),
    }
)
# Either a text match or one exact id. Canceling by TIME is the reason id exists: the
# YAML version deleted over a +/-2 minute window, so two reminders in the same minute
# died together. The caller picks the row it means and names it.
# Neither key is enforced here, so "you gave me nothing to match" is an answer rather
# than an abort. The handler MUST then refuse an empty match itself: an empty text would
# otherwise match every reminder, and cancel would take the lot.
CANCEL_SCHEMA = vol.Schema(
    {
        vol.Optional("target"): cv.string,
        vol.Optional("text"): cv.string,
        vol.Optional("id"): cv.string,
    }
)
# Like cancel: a text match, or one exact id. The dashboard card knows exactly which
# row was pressed and should not have to describe it back as a string.
SKIP_SCHEMA = vol.Schema(
    {
        vol.Optional("target"): cv.string,
        vol.Optional("text"): cv.string,
        vol.Optional("id"): cv.string,
        vol.Optional("scope", default="next"): vol.In(["next", "week"]),
    }
)
LIST_SCHEMA = vol.Schema(
    {
        vol.Optional("target"): cv.string,
        vol.Optional("days", default=7): vol.Coerce(int),
    }
)
FIND_SCHEMA = vol.Schema(
    {
        vol.Optional("target"): cv.string,
        vol.Optional("text"): cv.string,
        vol.Optional("start_date_time"): vol.Any(cv.datetime, cv.string),
        vol.Optional("end_date_time"): vol.Any(cv.datetime, cv.string),
    }
)
EDIT_SCHEMA = vol.Schema(
    {
        vol.Optional("target"): cv.string,
        vol.Required("id"): cv.string,
        vol.Optional("text"): cv.string,
        vol.Optional("due"): vol.Any(cv.datetime, cv.string),
        # An empty string is meaningful here - it clears the recurrence.
        vol.Optional("rrule"): vol.Any(cv.string, None),
    }
)


class ReminderManager:
    """Owns the store, the timers and delivery."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = ReminderStore(hass, entry.entry_id)
        self._unsub: dict[str, callable] = {}
        self._listeners: list[callable] = []

    @property
    def name(self) -> str:
        """Whose list this is. Falls back for an entry made before names existed."""
        return (self._opt(CONF_NAME) or self.entry.title or "Reminders").strip()

    def owns_satellite(self, entity_id: str) -> bool:
        return entity_id in (self._opt(CONF_ANNOUNCE_TARGETS) or [])

    # ---- options, all of which are hardcoded values in the YAML version ----

    def _opt(self, key: str, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    # ---- change notification, so the entities re-render ----

    def add_listener(self, cb) -> callable:
        self._listeners.append(cb)

        def _remove() -> None:
            self._listeners.remove(cb)

        return _remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # ---- scheduling ----

    def arm(self, reminder: Reminder) -> None:
        self.disarm(reminder.id)
        self._unsub[reminder.id] = async_track_point_in_time(
            self.hass, partial(self._fire, reminder.id), reminder.due
        )

    def disarm(self, reminder_id: str) -> None:
        if unsub := self._unsub.pop(reminder_id, None):
            unsub()

    def arm_all(self) -> list[Reminder]:
        """Arm every reminder still in the future. Returns the ones already overdue.

        Delivering the overdue ones is deliberately NOT done here - see async_catch_up.
        """
        now = dt_util.now()
        overdue: list[Reminder] = []
        for reminder in list(self.store.reminders.values()):
            if reminder.due > now:
                self.arm(reminder)
            else:
                overdue.append(reminder)
        return overdue

    async def async_catch_up(self, overdue: list[Reminder]) -> None:
        """Deliver what came due while we were down, or skip past it.

        A reminder inside the grace window is delivered LATE; anything older is advanced
        past, because a reminder from last week is noise rather than news.

        This must run after Home Assistant has STARTED, not during setup. At setup time
        the presence entity and the satellites have not restored yet, so the routing reads
        "not home" and a reminder that should have been spoken in the room is pushed to
        the phone instead. Measured on the first real test of this path: the reminder
        fired at 15:04:46 and the person entity did not get its state until 15:04:56 - the
        delivery was ten seconds too early to see that anybody was home.
        """
        now = dt_util.now()
        for reminder in overdue:
            late_by = (now - reminder.due).total_seconds()
            if late_by <= STARTUP_GRACE:
                _LOGGER.info(
                    "reminders: %r came due %.0fs ago, delivering late", reminder.text, late_by
                )
                await self._fire(reminder.id, now)
            else:
                _LOGGER.info("reminders: %r is %.0fs stale, skipping past", reminder.text, late_by)
                self._advance_past(reminder, now)

    def _advance_past(self, reminder: Reminder, now: datetime) -> None:
        """Move a stale reminder to its next future occurrence, or drop it."""
        nxt = reminder.next_occurrence(now) if reminder.recurring else None
        if nxt is None:
            _LOGGER.info("reminders: dropping stale %r", reminder.text)
            self.store.remove(reminder.id)
            self._notify()
            return
        reminder.due = nxt
        self.store.async_save()
        self.arm(reminder)

    async def _fire(self, reminder_id: str, _now) -> None:
        reminder = self.store.reminders.get(reminder_id)
        if reminder is None:
            return
        fired_for = reminder.due
        await self.deliver(reminder.text)
        reminder.last_fired = fired_for
        self.store.record_fired(reminder, fired_for)

        nxt = reminder.next_occurrence(fired_for)
        if nxt is None:
            self.store.remove(reminder.id)
            self.disarm(reminder.id)
        else:
            reminder.due = nxt
            self.store.async_save()
            self.arm(reminder)
        self._notify()

    # ---- delivery ----

    def _chime(self) -> tuple[bool, str | None]:
        """(play a sound, which one). None for the sound means assist_satellite's own.

        The setting has been three shapes across three versions - a media id, then a
        yes/no, now a named choice - and all three still have to resolve here, because
        an options form is not re-submitted just because the code changed underneath it.
        """
        # The toggle is the only thing that decides silence now; the picker says which
        # sound. An entry saved before the toggle existed has neither, so its stored
        # "none" still has to mean off.
        if not self._opt(CONF_CHIME_ON, DEFAULT_CHIME_ON):
            return False, None
        value = self._opt(CONF_CHIME, DEFAULT_CHIME)
        if value is True:
            return True, CHIME_URL
        if value is False:
            return False, None
        if isinstance(value, str) and value.strip() and value not in CHIME_CHOICES:
            return True, value          # a media id, from the first version
        choice = value if value in CHIME_CHOICES else DEFAULT_CHIME
        if choice == CHIME_NONE:
            return False, None
        if choice == CHIME_HA:
            return True, None
        if choice == CHIME_CUSTOM:
            # A leftover from when a sound was picked on a second page. The picker offers
            # the media folder directly now, so there is nothing left to look up - fall
            # back to the bundled sound rather than to silence, which was never asked for.
            return True, CHIME_URL
        return True, CHIME_URL

    async def deliver(self, text: str) -> None:
        """Speak it, or push it to a phone.

        Degrades rather than failing: with no satellites it notifies, with no notify
        service it announces, and with neither it falls back to a persistent
        notification. The YAML version assumed all three existed.
        """
        targets = self._opt(CONF_ANNOUNCE_TARGETS) or []
        notify_service = self._opt(CONF_NOTIFY_SERVICE)
        play, sound = self._chime()
        text = second_person(text)
        # "Reminder for John: take the trash out" once a second list exists. With one
        # list the name is noise - there is nobody else it could be for - and every
        # reminder anyone has already lived with would suddenly start announcing itself
        # differently.
        header = f"Reminder for {self.name}" if len(all_managers(self.hass)) > 1 else "Reminder"

        if targets and self._audible():
            data = {"message": f"{header}: {text}"}
            if play:
                data["preannounce"] = True
                # Naming no sound is how you ask for assist_satellite's OWN chime, so
                # that branch deliberately sets nothing else.
                if sound:
                    data["preannounce_media_id"] = sound
            else:
                # Explicitly off, not merely unset: assist_satellite plays its OWN
                # sound when preannounce is left alone, so silence has to be asked for.
                data["preannounce"] = False
            await self.hass.services.async_call(
                "assist_satellite", "announce", {"entity_id": targets, **data}, blocking=True
            )
            return

        entities, legacy = self._notify_targets(notify_service)
        if entities:
            # The modern notify platform: one call, however many recipients.
            await self.hass.services.async_call(
                "notify", "send_message",
                {"entity_id": entities, "title": header, "message": text},
                blocking=True,
            )
        for service in legacy:
            # A notify SERVICE rather than an entity - what a setting written before the
            # field became a list holds, and what plenty of integrations still expose.
            domain, name = service.split(".", 1)
            await self.hass.services.async_call(
                domain, name, {"title": header, "message": text}, blocking=True
            )
        if entities or legacy:
            return

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {"title": header, "message": text},
            blocking=True,
        )

    def _notify_targets(self, configured) -> tuple[list[str], list[str]]:
        """Split the setting into notify entities and legacy notify services."""
        if isinstance(configured, str):
            configured = [configured] if configured.strip() else []
        entities, legacy = [], []
        for target in configured or []:
            if not target or "." not in target:
                continue
            if target.startswith("notify.") and self.hass.states.get(target) is not None:
                entities.append(target)
            else:
                legacy.append(target)
        return entities, legacy

    def _audible(self) -> bool:
        """Home, and outside quiet hours."""
        presence = self._opt(CONF_PRESENCE_ENTITY)
        if presence and self.hass.states.get(presence):
            if self.hass.states.get(presence).state not in ("home", "on"):
                return False

        # No fallback to the built-in default: clearing the field in the options form is
        # how quiet hours are turned OFF, and reading a default here made that impossible
        # - the box came back filled in and the window silently stayed at 22:00-08:00.
        start = _parse_time(self._opt(CONF_QUIET_START))
        end = _parse_time(self._opt(CONF_QUIET_END))
        if start is None or end is None:
            return True
        now = dt_util.now().time()
        # Quiet hours normally wrap midnight, so the window is the union of both sides.
        quiet = (now >= start or now < end) if start > end else (start <= now < end)
        return not quiet

    # ---- operations, shared by the services and the intent handlers ----
    #
    # These live on the manager rather than in the service handlers so that a voice
    # intent and a service call cannot drift apart. The YAML version had exactly that
    # problem in reverse: the intent, the script and the confirmation each re-derived
    # the same lookup in Jinja, and fixing one left the others wrong.

    def schedule(self, text: str, due: datetime, rrule: str | None = None) -> Reminder:
        """Add a reminder and arm it. Raises InvalidRule or PastDue."""
        due = dt_util.as_local(due) if due.tzinfo else due.replace(
            tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
        if due <= dt_util.now():
            raise PastDue("That time has already passed")
        reminder = self.store.add(text, due, rrule)
        self.arm(reminder)
        self._notify()
        return reminder

    def cancel_reminders(self, reminders: list[Reminder]) -> int:
        """Remove reminders outright. For a series this takes every occurrence."""
        for reminder in reminders:
            self.disarm(reminder.id)
            self.store.remove(reminder.id)
        if reminders:
            self._notify()
        return len(reminders)

    def find_at(self, when: datetime, window: int = 90) -> list[Reminder]:
        """Reminders due within `window` seconds of `when`, soonest first."""
        when = dt_util.as_local(when) if when.tzinfo else when.replace(
            tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
        return sorted(
            (
                r for r in self.store.reminders.values()
                if abs((r.due - when).total_seconds()) < window
            ),
            key=lambda r: r.due,
        )

    def skip(self, text: str | None, scope: str, reminder_id: str | None = None) -> dict:
        """Skip occurrences without touching the series.

        Existence is established over every reminder, not over the skip window - asking
        "is there anything this week" and "does this reminder exist" are different
        questions, and conflating them told users a live daily reminder did not exist.
        """
        if reminder_id is not None:
            match = self.store.reminders.get(reminder_id)
            matches = [match] if match else []
        else:
            matches = self.store.find(text)
        recurring = [r for r in matches if r.recurring]
        if not recurring:
            if matches:
                return {"success": RESULT_NOT_RECURRING, "number": 0,
                        "error": f"{matches[0].text!r} happens only once - cancel it instead"}
            return {"success": RESULT_NO_MATCH, "number": 0,
                    "error": "No repeating reminder matched"}

        now = dt_util.now()
        skipped: list[datetime] = []
        ended = 0
        for reminder in recurring:
            if scope == "next":
                occurrences = [reminder.due]
            else:
                occurrences = self.store.occurrences_until(reminder, _end_of_week(now))
            for occurrence in occurrences:
                self.store.skip_occurrence(reminder, occurrence)
                skipped.append(occurrence)
            if reminder.id in self.store.reminders:
                self.arm(reminder)
            else:
                # Skipping the last occurrence exhausts the rule and the reminder goes.
                # The caller has to be told, or it reports that the series carries on.
                ended += 1
        self._notify()
        if not skipped:
            nxt = min(r.due for r in recurring)
            return {
                "success": RESULT_NONE_LEFT,
                "number": 0,
                "next": nxt,
                "error": "Nothing left to skip in that window",
            }
        return {"success": RESULT_OK, "number": len(skipped),
                "ended": ended, "occurrences": skipped}

    def search(
        self, text: str | None, start: datetime | None, end: datetime | None
    ) -> list[tuple[Reminder, list[datetime]]]:
        """Reminders matching a text and/or a date range, with the occurrences that hit.

        ONE entry per reminder however often it repeats - a weekly reminder inside a
        month-long range is one result with four occurrences, not four results. Which is
        why the occurrences come back alongside it: a range search on a series otherwise
        answers with `due`, the next occurrence overall, which may be nowhere near the
        range that was asked about.
        """
        candidates = self.store.find(text) if text else self.store.upcoming()
        if start is None and end is None:
            return [(r, None) for r in candidates]
        low = start or dt_util.now()
        high = end or (low + timedelta(days=3650))
        found = []
        for reminder in candidates:
            hits = [o for o in self.store.occurrences_until(reminder, high) if o >= low]
            if hits:
                found.append((reminder, hits))
        return found

    def edit(
        self,
        reminder: Reminder,
        text: str | None = None,
        due: datetime | None = None,
        rrule: str | None = None,
    ) -> Reminder:
        """Change a reminder in place. Raises InvalidRule or PastDue."""
        if due is not None:
            due = dt_util.as_local(due) if due.tzinfo else due.replace(
                tzinfo=dt_util.DEFAULT_TIME_ZONE
            )
            if due <= dt_util.now():
                raise PastDue("That time has already passed")
        rule = reminder.rrule
        if rrule is not None:
            # An empty string is how a series is turned back into a one-off.
            rule = normalize_rrule(rrule) if rrule.strip() else None

        if text:
            reminder.text = text
        if rrule is not None:
            reminder.rrule = rule
            # The old holes belonged to the old pattern; keeping them would silently
            # drop occurrences of the new one.
            reminder.exdates = []
        if due is not None:
            reminder.due = due
            # Re-anchor. Editing the time of a series means the series moves - otherwise
            # the rule regenerates the old time the next time it advances. Moving ONE
            # occurrence is what skip is for.
            reminder.dtstart = due
        elif rrule is not None:
            reminder.dtstart = reminder.due

        self.store.async_save()
        self.arm(reminder)
        self._notify()
        return reminder

    async def async_shutdown(self) -> None:
        for reminder_id in list(self._unsub):
            self.disarm(reminder_id)


def _as_dict(reminder: Reminder, occurrences: list[datetime] | None = None) -> dict:
    """One reminder as a response entry. The single place this shape is defined."""
    entry = {
        "id": reminder.id,
        "text": reminder.text,
        "due": reminder.due.isoformat(),
        "recurring": reminder.recurring,
        "rrule": reminder.rrule,
        "every": reminder.every,
        "until": reminder.until.isoformat() if reminder.until else None,
        "count": reminder.count,
        "is_last": reminder.is_last,
    }
    if occurrences is not None:
        entry["occurrences"] = [o.isoformat() for o in occurrences]
    return entry


def _parse_time(value):
    if not value:
        return None
    parsed = dt_util.parse_time(value)
    return parsed


def _end_of_week(now: datetime) -> datetime:
    """End of the coming Saturday; the week runs Sunday to Saturday."""
    to_sat = (5 - now.weekday()) % 7
    return (now + timedelta(days=to_sat)).replace(hour=23, minute=59, second=59, microsecond=0)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the voice intents as early as Home Assistant will let us.

    Deliberately here and not only in async_setup_entry. The entry loads behind slower
    integrations - zha, august, nest - and until it does, a sentence the grammar matches
    has no handler and comes back "Unknown intent CustomReminderCancel". That is a
    regression against the intent_script entries this replaced, which registered with
    the rest of the YAML config. The handlers cope with the manager not existing yet.
    """
    async_register_intents(hass)
    return True


def chimes_dir(hass: HomeAssistant) -> Path:
    """<config>/media/Chimes - where a user's own chimes go."""
    return Path(hass.config.path("media", CHIME_DIR))


async def _async_serve_chime(hass: HomeAssistant) -> None:
    """Publish the bundled chime and the user's chime folder, once.

    Registered here rather than in async_setup because async_setup only runs when the
    domain is present in configuration.yaml, and the chime has to work either way.

    The folder is created if it is missing. A static path cannot be registered over
    nothing, and registering happens at startup while dropping a file in happens later -
    so without this, making the folder would put sounds in the picker that 404 until the
    next restart.
    """
    if hass.data.get(CHIME_SERVED):
        return
    hass.data[CHIME_SERVED] = True
    folder = chimes_dir(hass)
    await hass.async_add_executor_job(partial(folder.mkdir, parents=True, exist_ok=True))
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                CHIME_URL, str(Path(__file__).parent / CHIME_FILE), cache_headers=True
            ),
            StaticPathConfig(CHIME_DIR_URL, str(folder), cache_headers=True),
        ]
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Reminders from a config entry."""
    await _async_serve_chime(hass)
    manager = ReminderManager(hass, entry)
    await manager.store.async_load()
    entry.runtime_data = manager
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    overdue = manager.arm_all()
    if overdue:
        # hass.is_running is True during CoreState.starting as well, which is the very
        # window this is avoiding - an entry that loads late would take the immediate
        # branch and deliver before presence has restored. Only fully running counts.
        if hass.state is CoreState.running:
            hass.async_create_task(manager.async_catch_up(overdue))
        else:

            async def _catch_up(_event) -> None:
                await manager.async_catch_up(overdue)

            entry.async_on_unload(
                hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _catch_up)
            )
    _register_services(hass)
    # Again, harmlessly: async_setup does not run when an entry is added to an already
    # running instance without the component having been set up first.
    async_register_intents(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the list's storage along with the entry.

    Without this, removing someone's list leaves their reminders on disk forever - and
    a future entry could never reach them, so it is dead weight rather than a safety net.
    """
    await ReminderStore(hass, entry.entry_id).async_remove()
    _LOGGER.info("reminders: removed the store for %s", entry.title)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager: ReminderManager = entry.runtime_data
    await manager.async_shutdown()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


def all_managers(hass: HomeAssistant) -> list:
    return list(hass.data.get(DOMAIN, {}).values())


# "Remind me to take my pills" is stored as you said it, but the house says it back TO
# you - so what leaves here is second person. Only at delivery: the store, the to-do list,
# the card and every text match keep your own words, because those are yours to read.
_SELF_WORDS = {
    "i": "you", "me": "you", "my": "your", "mine": "yours", "myself": "yourself",
    "i'm": "you're", "i've": "you've", "i'll": "you'll", "i'd": "you'd",
}
_SELF_RE = re.compile(r"\b(i'm|i've|i'll|i'd|i|me|my|mine|myself)\b", re.I)
_ARTICLES = {"the", "a", "an", "this", "that", "coal", "gold", "salt"}


def second_person(text: str) -> str:
    """First person to second: "take my pills" -> "take your pills"."""

    def swap(m: re.Match) -> str:
        word = m.group(0)
        if word.lower() == "mine":
            # A hole in the ground, not a possessive. "Inspect the mine" must survive.
            before = m.string[: m.start()].rstrip().rsplit(" ", 1)[-1].strip(".,;:").lower()
            if before in _ARTICLES:
                return word
        swapped = _SELF_WORDS[word.lower()]
        if word.isupper() and len(word) > 1:
            return swapped.upper()
        # "I" is capitalized wherever it stands, so its own case says nothing about where
        # the sentence begins - "check that I locked up" must not become "that You".
        # Every other word here is capitalized only when it really does start one.
        before = m.string[: m.start()].rstrip()
        starts = not before or before[-1] in ".!?"
        if word.lower().startswith("i") and not starts:
            return swapped
        return swapped[0].upper() + swapped[1:] if word[0].isupper() else swapped

    said = _SELF_RE.sub(swap, text or "")
    # "I am late" becomes "you am late" one word at a time, so the verb is fixed after.
    return re.sub(r"\b(you) am\b", r"\1 are", said, flags=re.I)


def sole_manager(hass: HomeAssistant):
    """The one list - when there IS only one. None once a second exists.

    There is deliberately no "default list" setting. With two people set up, quietly
    picking one for a request that named nobody is how a reminder lands on the wrong
    list with nobody told; the caller is asked for a name instead.
    """
    managers = all_managers(hass)
    return managers[0] if len(managers) == 1 else None


def manager_named(hass: HomeAssistant, target: str):
    """Find a list by name or by entry id. Case- and possessive-insensitive."""
    wanted = (target or "").strip().casefold().rstrip("'s").strip()
    for manager in all_managers(hass):
        if manager.entry.entry_id == target:
            return manager
        if manager.name.casefold() == wanted or manager.name.casefold().startswith(wanted):
            return manager
    return None


def _resolve(hass: HomeAssistant, call: ServiceCall):
    """The manager a call means. None when it cannot be settled - see _no_such_list.

    Not set up at all still raises, because that is a broken installation rather than an
    answerable question about somebody's reminders.
    """
    target = (call.data.get("target") or "").strip()
    if not all_managers(hass):
        raise ServiceValidationError("Reminders is not set up")
    return manager_named(hass, target) if target else sole_manager(hass)


def _no_such_list(target) -> dict:
    """Why no list was found: either the name is wrong, or none was given and it matters."""
    if not (target or "").strip():
        return {"success": RESULT_NO_WHOSE,
                "error": "More than one reminder list is set up - name whose in 'target'"}
    return {"success": RESULT_NO_LIST,
            "error": f"There is no reminder list called {target!r}"}


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "schedule"):
        return

    async def handle_schedule(call: ServiceCall) -> ServiceResponse:
        """Create a reminder. A bad time or rule is an ANSWER, not an exception.

        Raising would abort the calling script before it could read a response, which
        makes the failure invisible to exactly the caller best placed to handle it. The
        reason is logged as well, so it is still findable without one.
        """
        manager = _resolve(hass, call)
        if manager is None:
            return _no_such_list(call.data.get("target"))
        due = call.data["due"]
        if not isinstance(due, datetime):
            parsed = dt_util.parse_datetime(str(due))
            if parsed is None:
                _LOGGER.warning("reminders: could not read %r as a date and time", due)
                return {
                    "success": RESULT_BAD_TIME,
                    "error": f"Could not read {due!r} as a date and time",
                }
            due = parsed
        try:
            reminder = manager.schedule(call.data["text"], due, call.data.get("rrule"))
        except PastDue as err:
            _LOGGER.warning("reminders: %s (%s)", err, due)
            return {"success": RESULT_PAST, "error": str(err)}
        except InvalidRule as err:
            _LOGGER.warning("reminders: %s", err)
            return {"success": RESULT_BAD_RULE, "error": str(err)}
        return {"success": RESULT_OK, **_as_dict(reminder)}

    async def handle_cancel(call: ServiceCall) -> ServiceResponse:
        """Cancel by id, or by text. For a series this takes every occurrence, by design."""
        manager = _resolve(hass, call)
        if manager is None:
            return _no_such_list(call.data.get("target"))
        reminder_id = call.data.get("id")
        text = (call.data.get("text") or "").strip()
        if reminder_id is None and not text:
            # Refused explicitly, and this is load-bearing: an empty text matches EVERY
            # reminder, so falling through here would cancel the lot.
            return {
                "success": RESULT_NO_TARGET, "number": 0, "texts": [],
                "error": "Give either text or id",
            }
        if reminder_id is not None:
            match = manager.store.reminders.get(reminder_id)
            hits = [match] if match else []
        else:
            hits = manager.store.find(text)
        if not hits:
            return {
                "success": RESULT_NO_MATCH, "number": 0, "texts": [],
                "error": "No reminder matched",
            }
        texts = [r.text for r in hits]
        return {"success": RESULT_OK, "number": manager.cancel_reminders(hits),
                "texts": texts}

    async def handle_skip(call: ServiceCall) -> ServiceResponse:
        manager = _resolve(hass, call)
        if manager is None:
            return _no_such_list(call.data.get("target"))
        reminder_id = call.data.get("id")
        text = (call.data.get("text") or "").strip()
        if reminder_id is None and not text:
            return {
                "success": RESULT_NO_TARGET, "number": 0,
                "error": "Give either text or id",
            }
        result = manager.skip(text or None, call.data["scope"], reminder_id)
        # Datetimes go out as ISO strings; the manager keeps them as datetimes so the
        # intent handlers can format them without parsing their own output back.
        if "occurrences" in result:
            result["occurrences"] = [d.isoformat() for d in result["occurrences"]]
        if "next" in result:
            result["next"] = result["next"].isoformat()
        return result

    async def handle_list(call: ServiceCall) -> ServiceResponse:
        manager = _resolve(hass, call)
        if manager is None:
            return _no_such_list(call.data.get("target"))
        end = dt_util.now() + timedelta(days=call.data["days"])
        rows = [_as_dict(r) for r in manager.store.upcoming() if r.due <= end]
        if not rows:
            return {
                "success": RESULT_EMPTY, "count": 0, "reminders": [],
                "error": "No reminders in that window",
            }
        return {"success": RESULT_OK, "count": len(rows), "reminders": rows}

    async def handle_find(call: ServiceCall) -> ServiceResponse:
        """Search by text, by date range, or both. One entry per reminder."""
        manager = _resolve(hass, call)
        if manager is None:
            return _no_such_list(call.data.get("target"))
        text = (call.data.get("text") or "").strip() or None
        bounds = []
        for key in ("start_date_time", "end_date_time"):
            raw = call.data.get(key)
            if raw is None:
                bounds.append(None)
                continue
            when = raw if isinstance(raw, datetime) else dt_util.parse_datetime(str(raw))
            if when is None:
                return {
                    "success": RESULT_BAD_TIME, "count": 0, "reminders": [],
                    "error": f"Could not read {raw!r} as a date and time",
                }
            bounds.append(dt_util.as_local(when) if when.tzinfo else
                          when.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))
        start, end = bounds

        found = manager.search(text, start, end)
        if not found:
            filtered = text is not None or start is not None or end is not None
            return {
                "success": RESULT_NO_MATCH if filtered else RESULT_EMPTY,
                "count": 0,
                "reminders": [],
                "error": "Nothing matched" if filtered else "There are no reminders",
            }
        return {
            "success": RESULT_OK,
            "count": len(found),
            "reminders": [_as_dict(r, occ) for r, occ in found],
        }

    async def handle_edit(call: ServiceCall) -> ServiceResponse:
        """Change a reminder's text, time or recurrence. Answers like schedule does."""
        manager = _resolve(hass, call)
        if manager is None:
            return _no_such_list(call.data.get("target"))
        reminder = manager.store.reminders.get(call.data["id"])
        if reminder is None:
            return {"success": RESULT_NO_MATCH, "error": "No reminder with that id"}

        text = call.data.get("text")
        rrule = call.data.get("rrule")
        raw_due = call.data.get("due")
        if text is None and rrule is None and raw_due is None:
            return {
                "success": RESULT_NO_CHANGE,
                "error": "Give text, due or rrule - there is nothing to change",
            }

        due = None
        if raw_due is not None:
            due = raw_due if isinstance(raw_due, datetime) else dt_util.parse_datetime(
                str(raw_due)
            )
            if due is None:
                return {
                    "success": RESULT_BAD_TIME,
                    "error": f"Could not read {raw_due!r} as a date and time",
                }
        try:
            reminder = manager.edit(reminder, text, due, rrule)
        except PastDue as err:
            _LOGGER.warning("reminders: %s (%s)", err, raw_due)
            return {"success": RESULT_PAST, "error": str(err)}
        except InvalidRule as err:
            _LOGGER.warning("reminders: %s", err)
            return {"success": RESULT_BAD_RULE, "error": str(err)}
        return {"success": RESULT_OK, **_as_dict(reminder)}

    hass.services.async_register(
        DOMAIN, "schedule", handle_schedule, schema=SCHEDULE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "cancel", handle_cancel, schema=CANCEL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "skip", handle_skip, schema=SKIP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "list", handle_list, schema=LIST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "find", handle_find, schema=FIND_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "edit", handle_edit, schema=EDIT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
