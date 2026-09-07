"""Parsing the spoken answer to "edit my X reminder".

`python3 tests/test_edit_answer.py`. Stubs the Home Assistant modules intents.py imports.

The user is asked what the reminder should say and when, and answers in free speech -
there is no grammar to lean on, because the answer arrives through ask_question rather
than through a matched sentence. So this parser exists, and every case below is one it
has to get right or silently corrupt a reminder.
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
dt.DEFAULT_TIME_ZONE=LOCAL; dt.parse_datetime=lambda v: None; dt.parse_time=lambda v: None
dt.now=lambda: datetime.datetime.now(LOCAL); dt.as_local=lambda d: d

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

NOW=datetime.datetime(2026,9,5,14,0,tzinfo=LOCAL)   # a Saturday
DEFAULT=(8,15)
ok=True
def check(said, want_text, want_due, want_rrule):
    global ok
    t,d,r=it.parse_edit_answer(said, NOW, DEFAULT)
    got=(t, d.strftime("%Y-%m-%d %H:%M") if d else None, r)
    good = got==(want_text, want_due, want_rrule)
    ok = ok and good
    print(f" {'ok ' if good else 'FAIL'} {said!r:44} -> {got}")
    if not good: print(f"      wanted {(want_text, want_due, want_rrule)}")

print("=== text and a clock time ===")
check("walk the dog at 6 pm",           "walk the dog", "2026-09-05 18:00", None)
check("walk the dog at 6:30 pm",        "walk the dog", "2026-09-05 18:30", None)
check("take the bins out tomorrow at 8 am", "take the bins out", "2026-09-06 08:00", None)
check("call the vet on Thursday at 9 am",   "call the vet", "2026-09-10 09:00", None)
check("feed the cat tonight at 7",      "feed the cat", "2026-09-05 19:00", None)

print("=== time only - keeps the existing text ===")
check("at 6 pm",                        None, "2026-09-05 18:00", None)
check("tomorrow at 8 am",               None, "2026-09-06 08:00", None)

print("=== text only - keeps the existing time ===")
check("walk the dog",                   "walk the dog", None, None)
check("take pill 2",                    "take pill 2", None, None)   # a bare number is NOT a time

print("=== relative ===")
check("stretch in 20 minutes",          "stretch", "2026-09-05 14:20", None)
check("stretch in an hour",             "stretch", "2026-09-05 15:00", None)

print("=== recurring ===")
check("water the plants every Thursday at 6 pm", "water the plants", "2026-09-10 18:00", "FREQ=WEEKLY;BYDAY=TH")
check("take vitamins every day at 7 am",  "take vitamins", "2026-09-06 07:00", "FREQ=DAILY")
check("stand up every weekday",           "stand up", "2026-09-07 08:15", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
check("lock up every night",              "lock up", "2026-09-05 21:00", "FREQ=DAILY")

print("=== the three keywords - John's own examples ===")
# timing only, and a plural weekday IS a recurrence
check("change timing to 10 pm Sundays for next 4 weeks",
      None, "2026-09-06 22:00", "FREQ=WEEKLY;BYDAY=SU;UNTIL=20261003T235959")
check("change the timing to 6 pm",       None, "2026-09-05 18:00", None)
# text only - the time inside it is part of the NAME
check("change reminder to Watch Lanterns",        "Watch Lanterns", None, None)
check("change the reminder to Watch Lanterns at 9 pm", "Watch Lanterns at 9 pm", None, None)
check("rename it to Feed the cat",                "Feed the cat", None, None)
# both
check("replace with Watch Lanterns at 830 pm Sundays for next 5 weeks",
      "Watch Lanterns", "2026-09-06 20:30", "FREQ=WEEKLY;BYDAY=SU;UNTIL=20261010T235959")
check("replace it with walk the dog at 6 pm", "walk the dog", "2026-09-05 18:00", None)

print("=== compact times and plural days without a keyword ===")
check("walk the dog at 830 pm",          "walk the dog", "2026-09-05 20:30", None)
check("water the plants Tuesdays at 7 am", "water the plants", "2026-09-08 07:00", "FREQ=WEEKLY;BYDAY=TU")
check("stand up nightly",                "stand up", "2026-09-05 21:00", "FREQ=DAILY")

print("=== nothing ===")
check("",                                None, None, None)
check("change timing to whenever",       None, None, None)
print("ALL PASS" if ok else "*** SOME FAILED ***")
