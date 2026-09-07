"""The "When should this reminder occur?" question.

`python3 tests/test_ask_when.py`. Stubs the Home Assistant modules intents.py imports and
hands _ask_when_then_schedule a fake assist_satellite.

This is what lets a reminder be said in two breaths - the task, then the time - so the
cases that matter are the ones where the second breath is missing, late, or not a time at
all. Silence must not book the default on the first try; saying "default" must.
"""
import sys, types, datetime, importlib.util, asyncio

for name in ["homeassistant","homeassistant.core","homeassistant.exceptions",
             "homeassistant.helpers","homeassistant.helpers.storage","homeassistant.util",
             "homeassistant.util.dt","homeassistant.helpers.entity_registry",
             "homeassistant.helpers.intent","dateutil","dateutil.rrule"]:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.core"].HomeAssistant=object
class _HAError(Exception): pass
sys.modules["homeassistant.exceptions"].HomeAssistantError=_HAError
sys.modules["homeassistant.helpers.storage"].Store=object
sys.modules["homeassistant.helpers"].entity_registry=sys.modules["homeassistant.helpers.entity_registry"]
sys.modules["homeassistant.helpers"].intent=sys.modules["homeassistant.helpers.intent"]
class _IH: pass
sys.modules["homeassistant.helpers.intent"].IntentHandler=_IH
sys.modules["homeassistant.helpers.intent"].Intent=object
sys.modules["homeassistant.helpers.intent"].IntentResponse=object
sys.modules["homeassistant.helpers.intent"].async_register=lambda *a: None
LOCAL=datetime.timezone(datetime.timedelta(hours=-7))
NOW=datetime.datetime(2026,9,6,10,0,tzinfo=LOCAL)      # a Sunday, 10am
dt=sys.modules["homeassistant.util.dt"]
dt.DEFAULT_TIME_ZONE=LOCAL
dt.parse_time=lambda v: datetime.time(*map(int, v.split(":"))) if isinstance(v,str) else v
dt.parse_datetime=lambda v: None
dt.now=lambda: NOW
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
it.ASK_SETTLE=0

DEFAULT=it.Times((8,15), {"night": (22,30)})
QUESTION="When should this reminder occur? Default is 8:15 AM."


class FakeReminder:
    def __init__(self, due, rrule):
        self.due=due
        self.recurring=bool(rrule)
        self.every_base="every day" if rrule else ""
        self.every_bound=""

class FakeManager:
    def __init__(self): self.scheduled=[]
    def schedule(self, text, due, rrule=None):
        self.scheduled.append((text, due, rrule)); return FakeReminder(due, rrule)

class FakeHass:
    def __init__(self, replies):
        self.replies=list(replies); self.asked=[]; self.announced=[]; self.services=self
    async def async_call(self, domain, service, data, **kw):
        if service=="ask_question":
            self.asked.append(data["question"])
            reply=self.replies.pop(0) if self.replies else None
            if reply is None:
                raise _HAError("No answer from question")
            return {"sentence": reply}
        if service=="announce":
            self.announced.append(data["message"])
        return None


ok=True
def run(replies):
    manager=FakeManager(); hass=FakeHass(replies)
    asyncio.run(it._ask_when_then_schedule(
        hass, manager, "assist_satellite.kitchen", "water the ferns",
        QUESTION, DEFAULT, "you"))
    return manager, hass

def check(label, replies, *, due, asks, rrule=None, usual=False):
    global ok
    manager, hass = run(replies)
    bad=[]
    if not manager.scheduled:
        bad.append("nothing scheduled")
    else:
        text, when, rule = manager.scheduled[0]
        got=(when.hour, when.minute, when.day)
        if got != due: bad.append(f"due {got}, wanted {due}")
        if rule != rrule: bad.append(f"rrule {rule!r}, wanted {rrule!r}")
        if text != "water the ferns": bad.append(f"text {text!r}")
    if len(hass.asked)!=asks: bad.append(f"asked {len(hass.asked)}x, wanted {asks}")
    said = hass.announced[0] if hass.announced else ""
    if ("the usual then" in said) != usual:
        bad.append(f"default wording {'the usual then' in said}, wanted {usual}")
    if bad:
        ok=False; print(f"FAIL {label}\n      {'; '.join(bad)}\n      said: {said!r}")
    else:
        print(f"ok   {label}")

# a straight answer, first time
check("at 6 pm",            ["at 6 pm"],            due=(18,0,6),  asks=1)
check("in 20 minutes",      ["in 20 minutes"],      due=(10,20,6), asks=1)
check("a bare hour rolls",  ["at 9 am"],            due=(9,0,7),   asks=1)  # 9am today is gone
check("recurring answer",   ["every day at 7 am"],  due=(7,0,7),   asks=1, rrule="FREQ=DAILY")

# silence is NOT an answer - it asks again rather than booking the default
check("silent once",        [None, "at 6 pm"],      due=(18,0,6),  asks=2)
check("silent twice",       [None, None, "at 6 pm"],due=(18,0,6),  asks=3)
check("never answered",     [None, None, None],     due=(8,15,7),  asks=it.ASK_REPEATS, usual=True)

# an answer with no time in it is not one either
check("answer with no time",["um, whenever", "at 6 pm"], due=(18,0,6), asks=2)

# ...but "default" IS an answer, and it ends the asking at once
check("default",            ["default"],            due=(8,15,7),  asks=1, usual=True)
check("the default",        ["the default"],        due=(8,15,7),  asks=1, usual=True)
check("just default",       ["just default"],       due=(8,15,7),  asks=1, usual=True)

# the configured daypart is used, not the hardcoded 9pm
check("tonight uses config",["monday night"],       due=(22,30,7), asks=1)

# the repeat says sorry and asks the same thing
_, hass = run([None, "at 6 pm"])
if hass.asked[0]!=QUESTION:
    ok=False; print(f"FAIL first question: {hass.asked[0]!r}")
elif not hass.asked[1].startswith("Sorry - when should"):
    ok=False; print(f"FAIL repeat question: {hass.asked[1]!r}")
else:
    print("ok   repeat wording")

print("\nPASS" if ok else "\nFAILED"); sys.exit(0 if ok else 1)
