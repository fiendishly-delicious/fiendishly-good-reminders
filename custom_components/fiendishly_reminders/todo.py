"""ReminderListEntity - a reminder list, projected as a to-do list.

TodoListEntity is the base rather than a new `reminder.` domain. A custom integration
cannot add an entity platform, so inventing a domain would mean no dashboard card, no
services and no built-in list intents, all of which come free by subclassing. The class
name is ours; the platform is Home Assistant's.

What it shows is a projection, not the store. Each reminder appears ONCE, as its next
occurrence - a weekly reminder is one line reading "take the trash out, Thursday 6pm",
never fifty-two lines. Counting occurrences instead of reminders is a mistake worth
naming: the YAML version's cancel confirmation did it and was about to ask "are you sure
you want to cancel 52 reminders?".
"""

from __future__ import annotations

import logging

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_NAME, DOMAIN


def _device_name(manager) -> str:
    """"John" -> "John Reminders". Entity ids follow the DEVICE name, so this is what
    keeps two people's entities apart."""
    name = (manager._opt(CONF_NAME) or "").strip()
    return f"{name} Reminders" if name else "Reminders"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ReminderListEntity(entry.runtime_data, entry)])


class ReminderListEntity(TodoListEntity):
    """The reminder list."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = "mdi:bell-ring-outline"
    _attr_should_poll = False
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    )

    def __init__(self, manager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_reminders"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": _device_name(manager),
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._manager.add_listener(self.async_write_ha_state))

    @property
    def todo_items(self) -> list[TodoItem]:
        """One item per reminder, at its next occurrence, soonest first."""
        return [
            TodoItem(
                uid=r.id,
                summary=r.text,
                status=TodoItemStatus.NEEDS_ACTION,
                due=r.due,
                # Read-only prose. The rule itself is never exposed as editable text:
                # it is the one field a to-do item cannot represent, and letting it be
                # typed over in a card is a good way to lose a series to a typo.
                description=r.every or None,
            )
            for r in self._manager.store.upcoming()
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add a one-off reminder. Recurrence comes from voice or the service."""
        due = item.due
        if due is None:
            raise ValueError("A reminder needs a due date and time")
        if not isinstance(due, type(dt_util.now())):
            # A date rather than a datetime: a to-do card can offer either, and a
            # reminder with no time of day has nothing to fire at. Use the default.
            default = self._manager._opt("default_time", "08:15:00")
            parsed = dt_util.parse_time(default) or dt_util.parse_time("08:15:00")
            due = dt_util.start_of_local_day(due).replace(
                hour=parsed.hour, minute=parsed.minute
            )
        reminder = self._manager.store.add(item.summary, due)
        self._manager.arm(reminder)
        self.async_write_ha_state()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Rename or reschedule - and treat completion as a skip.

        Ticking off a recurring reminder means "done with this one", not "never again",
        so it drops that occurrence and lets the series carry on. That is exactly what
        exdates are for. Completing a one-off deletes it, which is the same thing.
        """
        reminder = self._manager.store.reminders.get(item.uid)
        if reminder is None:
            return

        if item.status == TodoItemStatus.COMPLETED:
            self._manager.disarm(reminder.id)
            if reminder.recurring:
                self._manager.store.skip_occurrence(reminder, reminder.due)
                if reminder.id in self._manager.store.reminders:
                    self._manager.arm(reminder)
            else:
                self._manager.store.remove(reminder.id)
            self.async_write_ha_state()
            return

        if item.summary:
            reminder.text = item.summary
        if item.due is not None and isinstance(item.due, type(dt_util.now())):
            reminder.due = dt_util.as_local(item.due)
            # Moving a one-off moves the anchor with it. Moving one occurrence of a
            # series deliberately does NOT: the rule keeps its own shape.
            if not reminder.recurring:
                reminder.dtstart = reminder.due
            self._manager.arm(reminder)
        self._manager.store.async_save()
        self.async_write_ha_state()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete outright. For a series this takes every occurrence, as intended."""
        for uid in uids:
            self._manager.disarm(uid)
            self._manager.store.remove(uid)
        self.async_write_ha_state()
