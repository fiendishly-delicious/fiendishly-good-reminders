"""Constants for the Reminders integration."""

from __future__ import annotations

DOMAIN = "fiendishly_reminders"
PLATFORMS = ["todo", "sensor"]

STORAGE_VERSION = 1
STORAGE_KEY = "fiendishly_reminders.data"

# Home Assistant validates CalendarEvent rrules against this set, and there is no
# reason for a reminder to repeat more often than daily anyway. Accepting FREQ=HOURLY
# into a calendar store is what made every read of calendar.reminders raise.
VALID_FREQS = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}

# Anything overdue by less than this when Home Assistant starts is still delivered,
# late, rather than dropped. Nothing else in the field does this: a reminder due while
# the box was rebooting is exactly the one you least want to lose.
STARTUP_GRACE = 3600  # seconds

# Every action answers with a `success` NUMBER and, when it is not 1, an `error` message.
# A number rather than a boolean so the reason travels with the outcome: a caller that
# only cares whether it worked tests `success == 1`, and one that wants to react to WHY
# has it without parsing the message. The codes are unique across all four actions, so
# the same check works whichever was called.
RESULT_OK = 1

# 1x - the reminder could not be created
RESULT_BAD_TIME = 10       # the due time could not be read at all
RESULT_BAD_RULE = 11       # the recurrence rule was rejected
RESULT_PAST = 12           # readable, but already gone by

# 2x - nothing to act on
RESULT_NO_TARGET = 20      # neither text nor id was given
RESULT_NO_MATCH = 21       # a target was given and matched nothing
RESULT_NO_CHANGE = 22      # an edit named a reminder but nothing to change about it
RESULT_NO_LIST = 23        # a list was named and there is no such list
RESULT_NO_WHOSE = 24       # more than one list and nothing said which        # a target was named and there is no such list

# 3x - matched, but the action does not apply
RESULT_NOT_RECURRING = 30  # skip on a one-off; there is no occurrence to drop
RESULT_NONE_LEFT = 31      # repeats, but nothing left inside the window

# 4x - nothing to return
RESULT_EMPTY = 40          # list found no reminders

CONF_NAME = "name"
CONF_ANNOUNCE_TARGETS = "announce_targets"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_PRESENCE_ENTITY = "presence_entity"
CONF_QUIET_START = "quiet_start"
CONF_QUIET_END = "quiet_end"
CONF_CHIME_ON = "chime_on"
CONF_CHIME = "chime"
CONF_CONFIRM_CANCEL = "confirm_cancel"
CONF_DEFAULT_TIME = "default_time"
# One per part of the day. These used to be hardcoded in intents.py, which meant "Monday
# night" was 9pm in every house whether or not anyone there was still up.
CONF_MORNING_TIME = "morning_time"
CONF_AFTERNOON_TIME = "afternoon_time"
# One setting for both words: "this evening" and "Monday night" are the same end of the
# day to everyone who is not writing a timetable.
CONF_EVENING_TIME = "evening_time"

# The chime ships with the integration and is served from here, so there is always a
# sound to fall back to even on a system with no media library at all.
CHIME_URL = "/fiendishly_reminders/chime.mp3"
CHIME_FILE = "chime.mp3"

# Your own sounds, dropped into <config>/media/Chimes. That folder is NOT the media
# library - Home Assistant's /media is a separate mount and media_source cannot see
# inside the config directory - so the integration serves it itself, the same way it
# serves the bundled chime.
CHIME_DIR = "Chimes"
CHIME_DIR_URL = "/fiendishly_reminders/chimes"
CHIME_SUFFIXES = (".mp3", ".wav", ".ogg", ".flac", ".m4a")

CHIME_FG = "fg"          # the sound that ships with this integration
CHIME_HA = "ha"          # whatever assist_satellite plays when preannounce is just "on"
CHIME_NONE = "none"      # silence. No longer offered in the picker - the toggle says it -
                         # but still read, because entries configured earlier hold it.
CHIME_CUSTOM = "custom"  # retired marker; entries saved while it existed still hold it
CHIME_CHOICES = [CHIME_FG, CHIME_HA, CHIME_NONE, CHIME_CUSTOM]

DEFAULT_CHIME = CHIME_FG
DEFAULT_CHIME_ON = True
DEFAULT_CONFIRM_CANCEL = True
DEFAULT_QUIET_START = "22:00:00"
DEFAULT_QUIET_END = "08:00:00"
DEFAULT_TIME = "08:15:00"
# The values that were hardcoded, so nothing changes for an entry that never sets them.
# Morning matches the plain default because that is what "tomorrow morning" already did.
DEFAULT_MORNING_TIME = "08:15:00"
DEFAULT_AFTERNOON_TIME = "14:00:00"
# 21:00 rather than the 19:00 evening used to mean: "Monday night" is the far commoner
# phrase of the two, and moving it would change reminders people already have.
DEFAULT_EVENING_TIME = "21:00:00"
