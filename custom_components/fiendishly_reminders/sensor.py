"""sensor.next_reminder - the next one due, with the whole list in attributes.

The to-do card cannot show which reminders repeat, because a TodoItem has no concept of
recurrence. This carries `every` as prose so a markdown card can say "every Thursday"
without re-deriving it in Jinja on every render.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NAME, DOMAIN


def _device_name(manager) -> str:
    """"John" -> "John Reminders". Entity ids follow the DEVICE name, so this is what
    keeps two people's entities apart."""
    name = (manager._opt(CONF_NAME) or "").strip()
    return f"{name} Reminders" if name else "Reminders"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([NextReminderSensor(entry.runtime_data, entry)])


class NextReminderSensor(SensorEntity):
    """When the next reminder is due."""

    _attr_has_entity_name = True
    _attr_name = "Next reminder"
    _attr_icon = "mdi:bell-clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = False

    def __init__(self, manager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_next_reminder"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": _device_name(manager),
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._manager.add_listener(self.async_write_ha_state))

    @property
    def native_value(self):
        upcoming = self._manager.store.upcoming()
        return upcoming[0].due if upcoming else None

    @property
    def extra_state_attributes(self):
        upcoming = self._manager.store.upcoming()
        store = self._manager.store
        return {
            # Whose list this is, so a caller holding only the entity id - the dashboard
            # card - can name it in `target` instead of relying on a default that no
            # longer exists.
            "list": self._manager.name,
            "count": len(upcoming),
            # What last fired, so "remind me again" and "cancel my last reminder" have
            # something to reach for once the reminder itself has gone.
            "last_text": store.last_text,
            "last_fired": store.last_fired.isoformat() if store.last_fired else None,
            "reminders": [
                {
                    "id": r.id,
                    "text": r.text,
                    "due": r.due.isoformat(),
                    "rrule": r.rrule,
                    "every": r.every,
                    "recurring": r.recurring,
                    "until": r.until.isoformat() if r.until else None,
                    "count": r.count,
                    "is_last": r.is_last,
                }
                for r in upcoming[:25]
            ],
        }
