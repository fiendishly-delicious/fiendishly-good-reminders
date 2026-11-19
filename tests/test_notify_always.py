"""Delivery: speak it, push it, or both.

`python3 tests/test_notify_always.py`. Stubs the Home Assistant modules __init__.py
imports and drives ReminderManager.deliver() against a fake hass.

Speaking and notifying have always been alternatives: whatever was announced ended
delivery, and the phone only ever heard a reminder that could not be said aloud. The
notify_always option adds the second channel ON TOP - the same reminder is pushed as
well, so being in a room without a satellite no longer means missing it. Off, delivery
has to behave exactly as it did before the option existed, which is the case an entry
configured earlier still hits.
"""
import sys, types, datetime, importlib.util, asyncio, pathlib, os

# ------------------------------------------------------------ home assistant stubs
for name in ["homeassistant", "homeassistant.components", "homeassistant.exceptions",
             "homeassistant.helpers", "homeassistant.util", "homeassistant.util.dt",
             "homeassistant.helpers.entity_registry", "homeassistant.helpers.intent",
             "dateutil", "dateutil.rrule"]:
    sys.modules.setdefault(name, types.ModuleType(name))

def _stub(name, **attrs):
    mod = sys.modules.setdefault(name, types.ModuleType(name))
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod

_stub("homeassistant.components.http", StaticPathConfig=object)
_stub("homeassistant.config_entries", ConfigEntry=object)
_stub("homeassistant.const", EVENT_HOMEASSISTANT_STARTED="homeassistant_started")

class _CoreState:
    running = "running"
class _SupportsResponse:
    OPTIONAL = "optional"
    ONLY = "only"
_stub("homeassistant.core", CoreState=_CoreState, HomeAssistant=object,
      ServiceCall=object, ServiceResponse=dict, SupportsResponse=_SupportsResponse)

class _HAError(Exception): pass
_stub("homeassistant.exceptions", HomeAssistantError=_HAError,
      ServiceValidationError=_HAError)

class _Store:                       # ReminderStore builds two of these on construction
    def __init__(self, *a, **k): pass
_stub("homeassistant.helpers.storage", Store=_Store)
_stub("homeassistant.helpers.event", async_track_point_in_time=lambda *a, **k: (lambda: None))
_stub("homeassistant.helpers.typing", ConfigType=dict)
_stub("homeassistant.helpers.intent", IntentHandler=object, Intent=object,
      IntentResponse=object, async_register=lambda *a: None)
sys.modules["homeassistant.helpers"].entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
sys.modules["homeassistant.helpers"].intent = sys.modules["homeassistant.helpers.intent"]

LOCAL = datetime.timezone(datetime.timedelta(hours=-7))
NOW = datetime.datetime(2026, 9, 6, 10, 0, tzinfo=LOCAL)          # a Sunday, 10am
dt = sys.modules["homeassistant.util.dt"]
dt.DEFAULT_TIME_ZONE = LOCAL
dt.now = lambda: NOW
dt.parse_time = lambda v: datetime.time(*map(int, v.split(":"))) if isinstance(v, str) else v
dt.parse_datetime = lambda v: None
dt.as_local = lambda d: d
sys.modules["homeassistant.util"].dt = dt
import dateutil.rrule as _rr
_rr.rrulestr = lambda *a, **k: None

class _CV:
    """config_validation, as much of it as a schema definition touches."""
    string = datetime = object
    @staticmethod
    def empty_config_schema(domain): return {}
_stub("homeassistant.helpers.config_validation", **{k: v for k, v in vars(_CV).items()
                                                   if not k.startswith("_")})
sys.modules["homeassistant.helpers"].config_validation = sys.modules["homeassistant.helpers.config_validation"]

class _V:
    """voluptuous, which is not needed for anything deliver() does."""
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
vol = types.ModuleType("voluptuous")
for _n in ("Schema", "Optional", "Required", "Any", "All", "In", "Coerce", "Range"):
    setattr(vol, _n, _V)
sys.modules["voluptuous"] = vol

# ---------------------------------------------------------------- the real modules
SRC = pathlib.Path(os.environ.get("FGR_SRC") or
                   pathlib.Path(__file__).resolve().parent.parent
                   / "custom_components" / "fiendishly_reminders")
pkg = types.ModuleType("fr"); pkg.__path__ = [str(SRC)]
sys.modules["fr"] = pkg
# intents is imported by __init__ for its side effect at setup; nothing here reaches it.
fake_intents = types.ModuleType("fr.intents")
fake_intents.async_register_intents = lambda hass: None
sys.modules["fr.intents"] = fake_intents
for m in ("const", "store", "__init__"):
    sp = importlib.util.spec_from_file_location("fr." + m, SRC / f"{m}.py")
    mod = importlib.util.module_from_spec(sp); sys.modules["fr." + m] = mod
    sp.loader.exec_module(mod)
cc = sys.modules["fr.__init__"]
C = sys.modules["fr.const"]

# ------------------------------------------------------------------------ the fakes
TEXT = "take my pills"
SATELLITES = ["assist_satellite.kitchen"]
PHONES = ["notify.mobile_app_pixel"]
LEGACY = ["notify.pushover"]


class FakeStates:
    def __init__(self, states): self._states = dict(states)
    def get(self, entity_id):
        state = self._states.get(entity_id)
        return None if state is None else types.SimpleNamespace(entity_id=entity_id, state=state)


class FakeHass:
    def __init__(self, states=None):
        self.calls = []                      # (service, data) in call order
        self.data = {}
        self.states = FakeStates(states or {})
        self.services = self
    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((f"{domain}.{service}", data))


class FakeEntry:
    def __init__(self, options):
        self.options = dict(options)
        self.data = {}
        self.entry_id = "e1"
        self.title = "John's Reminders"


def run(options, *, states=None, lists=1):
    hass = FakeHass(states)
    manager = cc.ReminderManager(hass, FakeEntry(options))
    hass.data[C.DOMAIN] = {"e1": manager}
    for n in range(1, lists):
        hass.data[C.DOMAIN][f"extra{n}"] = object()   # all_managers() only counts them
    asyncio.run(manager.deliver(TEXT))
    return hass


def spoken(hass):
    return [data for service, data in hass.calls if service == "assist_satellite.announce"]

def pushed(hass):
    return [data for service, data in hass.calls if service.startswith("notify.")]

def persistent(hass):
    return [data for service, data in hass.calls if service == "persistent_notification.create"]


ok = True
def check(label, options, *, states=None, lists=1,
          announce=0, notify=0, note=0, header="Reminder"):
    global ok
    hass = run(options, states=states, lists=lists)
    bad = []
    if len(spoken(hass)) != announce:
        bad.append(f"announced {len(spoken(hass))}x, wanted {announce}")
    if len(pushed(hass)) != notify:
        bad.append(f"notified {len(pushed(hass))}x, wanted {notify}")
    if len(persistent(hass)) != note:
        bad.append(f"persistent {len(persistent(hass))}x, wanted {note}")
    # Whatever went out has to be the same sentence on every channel, and to you.
    for data in spoken(hass):
        if data.get("message") != f"{header}: take your pills":
            bad.append(f"announce message {data.get('message')!r}")
    for data in pushed(hass):
        if data.get("message") != "take your pills":
            bad.append(f"push message {data.get('message')!r}")
        if data.get("title") != header:
            bad.append(f"push title {data.get('title')!r}")
    for data in persistent(hass):
        if data.get("message") != "take your pills":
            bad.append(f"persistent message {data.get('message')!r}")
    if bad:
        ok = False
        print(f"FAIL {label}\n      {'; '.join(bad)}")
    else:
        print(f"ok   {label}")

ALWAYS = {C.CONF_NOTIFY_ALWAYS: True}
NAMED = {C.CONF_NAME: "John"}
SPEAK_AND_PUSH = {**NAMED, C.CONF_ANNOUNCE_TARGETS: SATELLITES, C.CONF_NOTIFY_SERVICE: PHONES}
SPEAK_ONLY = {**NAMED, C.CONF_ANNOUNCE_TARGETS: SATELLITES}
PUSH_ONLY = {**NAMED, C.CONF_NOTIFY_SERVICE: PHONES}
AWAY = {"person.john": "not_home"}

# --- off, which is what every entry configured before the option existed gets --------
check("off: spoken, phones left alone", SPEAK_AND_PUSH, announce=1, notify=0)
check("off: spoken, nothing else to send", SPEAK_ONLY, announce=1, notify=0, note=0)
check("off: not spoken, phone hears it", PUSH_ONLY, states=AWAY, notify=1)

# --- on: the phone is a second channel, not a fallback ------------------------------
check("on: spoken AND pushed", {**SPEAK_AND_PUSH, **ALWAYS}, announce=1, notify=1)
check("on: legacy notify service too",
      {C.CONF_ANNOUNCE_TARGETS: SATELLITES, C.CONF_NOTIFY_SERVICE: LEGACY, **ALWAYS},
      announce=1, notify=1)
check("on: spoken with no recipients, no persistent on top",
      {**SPEAK_ONLY, **ALWAYS}, announce=1, notify=0, note=0)
check("on: not spoken still pushes",
      {**PUSH_ONLY, **ALWAYS, C.CONF_PRESENCE_ENTITY: "person.john"},
      states=AWAY, notify=1)
check("on: nowhere to go falls back to persistent",
      {**NAMED, **ALWAYS}, announce=0, notify=0, note=1)

# --- quiet hours are routing, not silencing: they must not change with the toggle ---
QUIET = {C.CONF_QUIET_START: "09:00:00", C.CONF_QUIET_END: "11:00:00"}   # now is 10am
check("off: quiet hours push it", {**SPEAK_AND_PUSH, **QUIET}, notify=1)
check("on: quiet hours still push it, once",
      {**SPEAK_AND_PUSH, **QUIET, **ALWAYS}, notify=1)
check("on: quiet hours and no phone -> persistent",
      {**SPEAK_ONLY, **QUIET, **ALWAYS}, note=1)

# --- presence is routing too --------------------------------------------------------
check("off: away pushes it",
      {**SPEAK_AND_PUSH, C.CONF_PRESENCE_ENTITY: "person.john"},
      states=AWAY, announce=0, notify=1)

# --- the one-chime rule is untouched: preannounce still follows the chime setting ----
hass = run({**SPEAK_AND_PUSH, **ALWAYS, C.CONF_CHIME_ON: False})
if spoken(hass) and spoken(hass)[0].get("preannounce") is False:
    print("ok   chime off still asks for silence")
else:
    ok = False; print(f"FAIL chime off: {spoken(hass)[0] if spoken(hass) else 'nothing spoken'!r}")

hass = run({**SPEAK_AND_PUSH, **ALWAYS})
if spoken(hass) and spoken(hass)[0].get("preannounce") is True:
    print("ok   chime on still asks for a sound")
else:
    ok = False; print(f"FAIL chime on: {spoken(hass)[0] if spoken(hass) else 'nothing spoken'!r}")

# --- two lists, so the reminder names whose it is - on both channels ----------------
check("on: two lists name the person",
      {**SPEAK_AND_PUSH, **ALWAYS}, lists=2, announce=1, notify=1,
      header="Reminder for John")

print("\nPASS" if ok else "\nFAILED"); sys.exit(0 if ok else 1)
