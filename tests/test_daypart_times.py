"""What "morning", "afternoon", "evening" and "night" mean.

`python3 tests/test_daypart_times.py`. Stubs the Home Assistant modules intents.py imports.

Each part of the day used to be a hardcoded hour, so "remind me Monday night" was 9pm in
every house. They are settings now, and the thing worth testing is that an unset one still
lands exactly where the hardcoded value did - nobody re-saves an options form because the
code changed underneath it.
"""
import sys, types, datetime, importlib.util

for name in ["homeassistant","homeassistant.core","homeassistant.exceptions",
             "homeassistant.helpers","homeassistant.helpers.storage","homeassistant.util",
             "homeassistant.util.dt","homeassistant.helpers.entity_registry",
             "homeassistant.helpers.intent","dateutil","dateutil.rrule"]:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.core"].HomeAssistant=object
sys.modules["homeassistant.exceptions"].HomeAssistantError=Exception
sys.modules["homeassistant.helpers.storage"].Store=object
sys.modules["homeassistant.helpers"].entity_registry=sys.modules["homeassistant.helpers.entity_registry"]
sys.modules["homeassistant.helpers"].intent=sys.modules["homeassistant.helpers.intent"]
class _IH: pass
sys.modules["homeassistant.helpers.intent"].IntentHandler=_IH
sys.modules["homeassistant.helpers.intent"].Intent=object
sys.modules["homeassistant.helpers.intent"].IntentResponse=object
sys.modules["homeassistant.helpers.intent"].async_register=lambda *a: None
LOCAL=datetime.timezone(datetime.timedelta(hours=-7))
dt=sys.modules["homeassistant.util.dt"]
dt.DEFAULT_TIME_ZONE=LOCAL
dt.parse_time=lambda v: datetime.time(*map(int, v.split(":"))) if isinstance(v,str) else v
dt.parse_datetime=lambda v: None
dt.now=lambda: datetime.datetime(2026,9,6,10,0,tzinfo=LOCAL)
dt.as_local=lambda d: d
import dateutil.rrule as _rr
_rr.rrulestr=lambda *a, **k: None

import os, pathlib
# Set FGR_SRC to test a copy somewhere else - a live /config, say. Otherwise this
# checkout is what gets tested.
SRC = pathlib.Path(os.environ.get("FGR_SRC") or
                   pathlib.Path(__file__).resolve().parent.parent
                   / "custom_components" / "fiendishly_reminders")
pkg=types.ModuleType("fr"); pkg.__path__=[str(SRC)]
sys.modules["fr"]=pkg
for m in ("const","store","intents"):
    sp=importlib.util.spec_from_file_location("fr."+m, SRC / f"{m}.py")
    mod=importlib.util.module_from_spec(sp); sys.modules["fr."+m]=mod; sp.loader.exec_module(mod)
it=sys.modules["fr.intents"]

NOW=datetime.datetime(2026,9,6,10,0,tzinfo=LOCAL)     # a Sunday, 10am
BARE=(8,15)                                            # a plain tuple, as callers used to pass
ok=True

def slots(**kw):
    return {k: {"value": v} for k, v in kw.items()}

def check(label, got, want):
    global ok
    if got != want:
        ok=False; print(f"FAIL {label}\n      got {got}, wanted {want}")
    else:
        print(f"ok   {label}")

def at(when):
    return None if when is None else (when.hour, when.minute, when.day)

# ---------------------------------------------------------------- the built-in values
# An unset daypart has to land where the hardcoded one did.
check("afternoon, unset", at(it.when_from_slots(NOW, slots(weekday=0, daypart="afternoon"), BARE)), (14,0,7))
check("evening, unset",   at(it.when_from_slots(NOW, slots(weekday=0, daypart="evening"),   BARE)), (19,0,7))
check("night, unset",     at(it.when_from_slots(NOW, slots(weekday=0, daypart="night"),     BARE)), (21,0,7))
# Morning has no built-in entry ON PURPOSE - it falls through to the plain default.
check("morning, unset",   at(it.when_from_slots(NOW, slots(weekday=0, daypart="morning"),   BARE)), (8,15,7))
check("no daypart",       at(it.when_from_slots(NOW, slots(weekday=0),                      BARE)), (8,15,7))

# --------------------------------------------------------------- the configured values
T = it.Times((9, 30), {"morning": (6,45), "afternoon": (13,15),
                       "evening": (18,0), "night": (22,30)})
check("morning, set",   at(it.when_from_slots(NOW, slots(weekday=0, daypart="morning"),   T)), (6,45,7))
check("afternoon, set", at(it.when_from_slots(NOW, slots(weekday=0, daypart="afternoon"), T)), (13,15,7))
check("evening, set",   at(it.when_from_slots(NOW, slots(weekday=0, daypart="evening"),   T)), (18,0,7))
check("night, set",     at(it.when_from_slots(NOW, slots(weekday=0, daypart="night"),     T)), (22,30,7))
check("no daypart, set",at(it.when_from_slots(NOW, slots(weekday=0),                      T)), (9,30,7))

# "tomorrow morning" is its own branch and used to read the PLAIN default
check("tomorrow morning, unset", at(it.when_from_slots(NOW, slots(tomorrow_morning="yes"), BARE)), (8,15,7))
check("tomorrow morning, set",   at(it.when_from_slots(NOW, slots(tomorrow_morning="yes"), T)),    (6,45,7))

# A daypart still only disambiguates a SPOKEN hour - it must not override one.
check("night at 9 -> 21:00", at(it.when_from_slots(NOW, slots(weekday=0, daypart="night", hour=9), T)), (21,0,7))
check("morning at 7 -> 07:00", at(it.when_from_slots(NOW, slots(weekday=0, daypart="morning", hour=7), T)), (7,0,7))

# Times IS a tuple, which is what keeps every other caller and test working
check("Times unpacks", tuple(T), (9,30))
check("recurring uses it", (lambda r: (r[0].hour, r[0].minute))(
    it.recurrence_from_slots(NOW, slots(recur="weekly", weekday=6, daypart="night"), "", T)), (22,30))

# ------------------------------------------------- editing keeps the reminder's own time
# "Make it repeat every Tuesday" says nothing about moving it to the default hour, so the
# fallback when editing is the reminder's CURRENT time - built the same way the handler
# builds it, from the existing due time plus the configured dayparts.
KEEP = it.Times((17, 0), T.parts)          # a reminder currently at 5pm

def first_at(slots, times):
    got = it.recurrence_from_slots(NOW, slots, "", times)
    return None if got is None else (got[0].hour, got[0].minute)

check("pattern only keeps 5pm",  first_at(slots(recur="weekly", weekday=1), KEEP), (17, 0))
check("pattern only, was default", first_at(slots(recur="weekly", weekday=1), T), (9, 30))
# A spoken hour still wins over the kept time...
check("spoken hour wins", first_at(slots(recur="weekly", weekday=1, hour=7, ampm="am"), KEEP), (7, 0))
# ...and so does a part of the day, because both are read before the fallback.
check("daypart wins", first_at(slots(recur="weekly", weekday=1, daypart="morning"), KEEP), (6, 45))
# Moving to a named day with no hour keeps it too - you said which day, not which hour.
check("move to a weekday keeps 5pm",
      at(it.when_from_slots(NOW, slots(weekday=3), KEEP)), (17, 0, 10))

print("\nPASS" if ok else "\nFAILED"); sys.exit(0 if ok else 1)
