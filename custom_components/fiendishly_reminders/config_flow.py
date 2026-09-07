"""Config flow - the questions that turn one person's setup into anybody's.

Every field here is a hardcoded entity id or literal in the YAML version. That list is
exactly what fell out of extracting the delivery script: whatever delivery depended on
is what has to be asked.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.data_entry_flow import section
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CHIME_DIR_URL,
    CHIME_FG,
    CHIME_SUFFIXES,
    CHIME_HA,
    CHIME_NONE,
    CONF_ANNOUNCE_TARGETS,
    CONF_CHIME_ON,
    CONF_NAME,
    CONF_CHIME,
    CONF_CONFIRM_CANCEL,
    CONF_AFTERNOON_TIME,
    CONF_DEFAULT_TIME,
    CONF_EVENING_TIME,
    CONF_MORNING_TIME,
    CONF_NOTIFY_SERVICE,
    CONF_PRESENCE_ENTITY,
    CONF_QUIET_END,
    CONF_QUIET_START,
    DEFAULT_CHIME,
    DEFAULT_CONFIRM_CANCEL,
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    DEFAULT_AFTERNOON_TIME,
    DEFAULT_EVENING_TIME,
    DEFAULT_MORNING_TIME,
    DEFAULT_TIME,
    DOMAIN,
)


# Which fields live under which heading. The form is one flat set of options underneath -
# sections only group the UI - so this table is also what flattens a submission back out.
SECTIONS: dict[str, tuple[str, ...]] = {
    "whose": (CONF_NAME,),
    "delivery": (CONF_ANNOUNCE_TARGETS, CONF_NOTIFY_SERVICE),
    "quiet": (CONF_PRESENCE_ENTITY, CONF_QUIET_START, CONF_QUIET_END),
    "chime": (CONF_CHIME_ON, CONF_CHIME),
    "times": (CONF_DEFAULT_TIME, CONF_MORNING_TIME, CONF_AFTERNOON_TIME, CONF_EVENING_TIME),
    "confirmations": (CONF_CONFIRM_CANCEL,),
}
# Cleared rather than absent. A vol.Optional the user emptied simply does not come back,
# which is indistinguishable from one they never touched - so these are written as "" on
# the way out, and nothing may read them with a fallback default. That is what made
# clearing quiet hours look like it worked and then not stick.
CLEARABLE = (CONF_PRESENCE_ENTITY, CONF_QUIET_START, CONF_QUIET_END)


def _fields(defaults: dict, chimes: list) -> dict[str, dict]:
    """Every field, by section. One place, so the two flows cannot drift."""
    return {
        "whose": {
            # Whose list this is. Also what a spoken "remind Jen to ..." matches on, and
            # what names the entities, so it is worth being the first thing asked.
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")):
                selector.TextSelector(),
        },
        "delivery": {
            vol.Optional(
                CONF_ANNOUNCE_TARGETS, default=defaults.get(CONF_ANNOUNCE_TARGETS, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="assist_satellite", multiple=True)
            ),
            # A list, and pickable, for the same reason the satellites are: a household
            # has more than one phone in it.
            vol.Optional(
                CONF_NOTIFY_SERVICE, default=_notify_default(defaults)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="notify", multiple=True)
            ),
        },
        "quiet": {
            # suggested_value, NOT default: these are the clearable ones, and a default
            # would put the value straight back the moment the box was emptied.
            vol.Optional(
                CONF_PRESENCE_ENTITY,
                description={"suggested_value": defaults.get(CONF_PRESENCE_ENTITY) or None},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="person")),
            vol.Optional(
                CONF_QUIET_START,
                description={"suggested_value": defaults.get(CONF_QUIET_START) or None},
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_QUIET_END,
                description={"suggested_value": defaults.get(CONF_QUIET_END) or None},
            ): selector.TimeSelector(),
        },
        "chime": {
            vol.Required(
                CONF_CHIME_ON, default=_chime_on_default(defaults)
            ): selector.BooleanSelector(),
            # Labels are spelled out here rather than left to a translation key, so what
            # the picker shows cannot depend on a translation file resolving.
            vol.Optional(
                CONF_CHIME, default=_chime_default(defaults, chimes)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=chimes,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        },
        "times": {
            # Required, and not because a value has to be typed - they are pre-filled -
            # but because ha-base-time-input draws its clear button only when the field
            # is optional, and there is no such thing as "no morning".
            vol.Required(
                CONF_DEFAULT_TIME, default=defaults.get(CONF_DEFAULT_TIME, DEFAULT_TIME)
            ): selector.TimeSelector(),
            vol.Required(
                CONF_MORNING_TIME,
                default=defaults.get(CONF_MORNING_TIME, DEFAULT_MORNING_TIME),
            ): selector.TimeSelector(),
            vol.Required(
                CONF_AFTERNOON_TIME,
                default=defaults.get(CONF_AFTERNOON_TIME, DEFAULT_AFTERNOON_TIME),
            ): selector.TimeSelector(),
            vol.Required(
                CONF_EVENING_TIME,
                default=defaults.get(CONF_EVENING_TIME, DEFAULT_EVENING_TIME),
            ): selector.TimeSelector(),
        },
        "confirmations": {
            vol.Optional(
                CONF_CONFIRM_CANCEL,
                default=bool(defaults.get(CONF_CONFIRM_CANCEL, DEFAULT_CONFIRM_CANCEL)),
            ): selector.BooleanSelector(),
        },
    }


def _schema(defaults: dict, chimes: list) -> vol.Schema:
    """The form, in sections.

    A section is the only way to put a heading ABOVE a field: ha-form gives a text or
    dropdown selector its title as a Material floating label, inside the box.

    One delivery channel is required; everything else degrades rather than failing.
    Satellites OR a notify service - either will do, and neither alone is the point. What
    a setup cannot be is deaf and mute at once, because then a reminder has nowhere to go
    and firing it exactly on time achieves nothing.
    """
    fields = _fields(defaults, chimes)
    return vol.Schema(
        {
            vol.Required(name): section(vol.Schema(schema), {"collapsed": False})
            for name, schema in fields.items()
        }
    )


def _flatten(user_input: dict) -> dict:
    """Sections back to one flat set of options, with cleared fields written as "".

    Everything downstream - the manager, the entities, the store - reads flat keys, and
    should not have to know that the FORM happens to be grouped.
    """
    flat: dict = {}
    for section_name, keys in SECTIONS.items():
        values = user_input.get(section_name) or {}
        for key in keys:
            if key in values:
                flat[key] = values[key]
            elif key in CLEARABLE:
                flat[key] = ""
    return flat



BUILT_IN_CHIMES = {
    CHIME_FG: "fgReminders - Fiendishly Good Reminders default chime",
    CHIME_HA: "Home Assistant's own chime",
}
def _list_chimes(folder: str | None) -> list:
    """The picker: the two built-in sounds, then every file in <config>/media/Chimes.

    Reading a folder rather than asking for a media id keeps the whole thing one dropdown -
    no second page, nothing to fill in wrongly - and it works for a folder inside the
    config directory, which media_source cannot see into at all. The integration serves
    that folder itself, so the value here is the URL the file is published at.
    """
    options = [
        selector.SelectOptionDict(value=value, label=label)
        for value, label in BUILT_IN_CHIMES.items()
    ]
    if not folder:
        return options
    try:
        found = sorted(
            entry for entry in Path(folder).iterdir()
            if entry.is_file() and entry.suffix.lower() in CHIME_SUFFIXES
        )
    except OSError:
        return options                       # no folder, or not readable. Not an error.
    options.extend(
        selector.SelectOptionDict(
            # quote, because the filename is the user's and "Test Bell.mp3" is normal.
            value=f"{CHIME_DIR_URL}/{quote(entry.name)}",
            label=entry.stem,
        )
        for entry in found
    )
    return options


async def _chimes_for(hass) -> list:
    """The picker's options. The folder is read off the event loop."""
    from . import chimes_dir

    return await hass.async_add_executor_job(_list_chimes, str(chimes_dir(hass)))


def _chime_on_default(defaults: dict) -> bool:
    """Whether a chime plays at all. "none" was how that was said before the toggle."""
    if CONF_CHIME_ON in defaults:
        return bool(defaults[CONF_CHIME_ON])
    value = defaults.get(CONF_CHIME, DEFAULT_CHIME)
    return not (value is False or value == CHIME_NONE)


def _chime_default(defaults: dict, chimes: list) -> str:
    """Read whatever shape the chime setting was saved in back as one of the choices.

    An entry configured before this was a choice holds True/False, and the very first
    version held a media id - a SelectSelector rejects all three, so the form would not
    open at all without this.
    """
    value = defaults.get(CONF_CHIME, DEFAULT_CHIME)
    # A stored sound whose file has since been deleted would not be in the picker, and a
    # SelectSelector rejects a default it does not offer - the form would refuse to open.
    if value in {option["value"] for option in chimes}:
        return value
    return DEFAULT_CHIME


def _title(name: str) -> str:
    """"John" -> "John's Reminders"; a name already ending in s takes a bare apostrophe."""
    return f"{name}' Reminders" if name.endswith(("s", "S")) else f"{name}'s Reminders"


def _notify_default(defaults: dict) -> list[str]:
    """Existing settings held a single service name; the field is now a list."""
    value = defaults.get(CONF_NOTIFY_SERVICE) or []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return list(value)


def _missing(user_input: dict) -> str | None:
    """Which required things were left out, as one error key naming all of them.

    Reported together rather than one at a time: being sent back twice for two empty
    boxes on the same page is worse than being told both at once. Nothing is saved until
    they are filled, and the form comes straight back with everything else intact.
    """
    no_name = not (user_input.get(CONF_NAME) or "").strip()
    no_delivery = _no_delivery(user_input)
    if no_name and no_delivery:
        return "no_name_or_delivery"
    if no_name:
        return "no_name"
    if no_delivery:
        return "no_delivery"
    return None


def _no_delivery(user_input: dict) -> bool:
    """True when a reminder would have nowhere to go.

    Checked by hand because voluptuous validates fields one at a time, and this is a
    condition across two of them - a multiple-entity selector also hands back an empty
    list rather than omitting the key.
    """
    notify = user_input.get(CONF_NOTIFY_SERVICE) or []
    if isinstance(notify, str):
        notify = [notify] if notify.strip() else []
    return not user_input.get(CONF_ANNOUNCE_TARGETS) and not notify


class RemindersConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set Reminders up."""

    VERSION = 1
    async def async_step_user(self, user_input=None):
        """Set up one person's list. Run it again for the next person."""
        errors = {}
        if user_input is not None:
            user_input = _flatten(user_input)
            if problem := _missing(user_input):
                errors["base"] = problem
            else:
                user_input[CONF_NAME] = user_input[CONF_NAME].strip()
                return self._done(user_input)
        # The form comes back holding what was typed, not emptied - a rejected submission
        # is a correction, and re-entering the other five sections is not part of it.
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}, await _chimes_for(self.hass)),
            errors=errors,
        )

    def _done(self, options: dict):
        return self.async_create_entry(
            title=_title(options[CONF_NAME]), data={}, options=options
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return RemindersOptionsFlow()


class RemindersOptionsFlow(OptionsFlow):
    """Change any of it later without a restart - the entry reloads instead."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input = _flatten(user_input)
            if problem := _missing(user_input):
                errors["base"] = problem
            else:
                user_input[CONF_NAME] = user_input[CONF_NAME].strip()
                return self._done(user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(
                user_input or dict(self.config_entry.options),
                await _chimes_for(self.hass),
            ),
            errors=errors,
        )

    def _done(self, options: dict):
        self.hass.config_entries.async_update_entry(
            self.config_entry, title=_title(options[CONF_NAME])
        )
        return self.async_create_entry(data=options)
