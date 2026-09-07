"""The spoken "Who's this reminder for?" question.

`python3 tests/test_ask_whose.py`. Stubs the Home Assistant modules intents.py imports
and hands _ask_whose_then_schedule a fake assist_satellite.

The point of the test is that the question has NO deadline: silence is never read as an
answer, so an unanswered attempt is asked again. What ends it is a count of spoken
attempts, and then NOTHING is created - with two people set up there is no safe guess
about whose reminder this is, and one on the wrong list is worse than one not made.
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
dt=sys.modules["homeassistant.util.dt"]
dt.DEFAULT_TIME_ZONE=LOCAL; dt.parse_datetime=lambda v: None; dt.parse_time=lambda v: None
dt.now=lambda: datetime.datetime(2026,9,6,14,0,tzinfo=LOCAL); dt.as_local=lambda d: d
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
it.ASK_SETTLE=0                      # the test is about repeats, not about waiting

DUE=datetime.datetime(2026,9,7,17,0,tzinfo=LOCAL)


class FakeReminder:
    recurring=False; every_base=""; every_bound=""
    def __init__(self,due): self.due=due

class FakeManager:
    def __init__(self,name,entry_id):
        self.name=name; self.scheduled=[]
        self.entry=types.SimpleNamespace(entry_id=entry_id)
    def schedule(self,text,due,rrule=None):
        self.scheduled.append(text); return FakeReminder(due)

class FakeHass:
    """Answers ask_question from a script of replies, and records what was said.

    A reply of None raises, which is what a turn that ended in silence does. A string
    reply is a raw transcript; a dict is a matched answer.
    """
    def __init__(self,replies):
        self.replies=list(replies); self.asked=[]; self.announced=[]
        self.services=self
    async def async_call(self,domain,service,data,**kw):
        if service=="ask_question":
            self.asked.append(data["question"])
            reply=self.replies.pop(0) if self.replies else None
            if reply is None:
                raise _HAError("No answer from question")
            return {"sentence": reply} if isinstance(reply, str) else reply
        if service=="announce":
            self.announced.append(data["message"])
        return None


ok=True
def run(replies):
    john=FakeManager("John","e_john"); jen=FakeManager("Jen","e_jen")
    hass=FakeHass(replies)
    fr=sys.modules["fr"]
    fr.default_manager=lambda h: john
    asyncio.run(it._ask_whose_then_schedule(
        hass,"assist_satellite.kitchen",[john,jen],"feed the cat",DUE,None))
    return john,jen,hass

def check(label,replies,*,lands_on,asks,says_fallback):
    global ok
    john,jen,hass=run(replies)
    got_on = "Jen" if jen.scheduled else ("John" if john.scheduled else None)
    said = hass.announced[0] if hass.announced else ""
    # Giving up has to SAY it gave up, or silence and a misfiled reminder are the same.
    gave_up = "don't know whose" in said
    bad=[]
    if got_on!=lands_on: bad.append(f"landed on {got_on}, wanted {lands_on}")
    if len(hass.asked)!=asks: bad.append(f"asked {len(hass.asked)}x, wanted {asks}")
    if gave_up!=says_fallback: bad.append(f"gave-up wording {gave_up}, wanted {says_fallback}")
    if bad:
        ok=False; print(f"FAIL {label}\n      {'; '.join(bad)}\n      said: {said!r}")
    else:
        print(f"ok   {label}")

# answered first time - one question, no fallback wording
check("answers straight away", [{"id":"e_jen"}],
      lands_on="Jen", asks=1, says_fallback=False)

# silence then an answer - the slow answer is HONOURED, not overridden by a timeout
check("silent once, then answers", [None,{"id":"e_jen"}],
      lands_on="Jen", asks=2, says_fallback=False)
check("silent twice, then answers", [None,None,{"id":"e_john"}],
      lands_on="John", asks=3, says_fallback=False)

# never answered - asks ASK_REPEATS times, then creates NOTHING and says so
check("never answered", [None,None,None],
      lands_on=None, asks=it.ASK_REPEATS, says_fallback=True)

# spoke, but matched no canned sentence: the raw transcript still carries the name
check("unmatched sentence naming one list", [{"sentence":"that's Jennifer's I think"}],
      lands_on="Jen", asks=1, says_fallback=False)
check("raw name only", [{"sentence":"jen"}],
      lands_on="Jen", asks=1, says_fallback=False)

# ...but a transcript naming BOTH is not an answer - ask again rather than guess
check("transcript names both", [{"sentence":"John or Jen?"},{"id":"e_jen"}],
      lands_on="Jen", asks=2, says_fallback=False)

# BOTH asks name the options - you cannot answer a question whose choices you have to
# guess, and hearing them named is what makes the answer land on a known phrasing.
def asked(replies):
    return run(replies)[2].asked

def wording(label, replies, index, want):
    global ok
    got = asked(replies)[index]
    if got != want:
        ok=False; print(f"FAIL {label}\n      got  {got!r}\n      want {want!r}")
    else:
        print(f"ok   {label}")

wording("first ask names the options", [None,{"id":"e_jen"}], 0,
        "Who's this reminder for? John or Jen?")
# Silence and a mis-hear are different failures and must not sound the same: one is
# "say something", the other is "the name never arrived".
wording("silence -> didn't get that", [None,{"id":"e_jen"}], 1,
        "I didn't get that. Was it John or Jen?")
wording("mis-hear -> quotes it back", [{"sentence":"james."},{"id":"e_jen"}], 1,
        "I heard james. Was it John or Jen?")

# A near miss on the name IS an answer - "Ozzie" for "Ozzy", "Jenn" for "Jen".
check("near miss on the name", [{"sentence":"Jenn"}],
      lands_on="Jen", asks=1, says_fallback=False)
check("nothing like either name", [{"sentence":"james."},{"sentence":"james."},{"sentence":"james."}],
      lands_on=None, asks=3, says_fallback=True)

# ------------------------------------------------- both questions, in one exchange
# A reminder that gave neither a name nor a time needs both asked. Whose comes first
# because the second question's answer depends on it: the default hour it offers is
# the CHOSEN list's, not a guess.
class FakeHandler:
    """Only the three things _ask_whose_then_when reads off the intent handler."""
    def _default_time(self, manager): return it.Times((8, 15), {})
    def _default_time_phrase(self, manager): return "8:15 AM"
    def _whose(self, hass, manager): return manager.name

def both(replies):
    john=FakeManager("John","e_john"); jen=FakeManager("Jen","e_jen")
    hass=FakeHass(replies)
    asyncio.run(it._ask_whose_then_when(
        hass, FakeHandler(), "assist_satellite.kitchen", [john, jen], "water the ferns"))
    return john, jen, hass

def check_both(label, replies, *, lands_on, asks):
    global ok
    john, jen, hass = both(replies)
    got = "Jen" if jen.scheduled else ("John" if john.scheduled else None)
    bad=[]
    if got != lands_on: bad.append(f"landed on {got}, wanted {lands_on}")
    if hass.asked != asks: bad.append(f"asked {hass.asked}\n      wanted {asks}")
    if bad: ok=False; print(f"FAIL {label}\n      {'; '.join(bad)}")
    else: print(f"ok   {label}")

check_both("whose, then when", [{"id":"e_jen"}, "at 6 pm"],
           lands_on="Jen",
           asks=["Who's this reminder for? John or Jen?",
                 "When should this reminder occur? Default is 8:15 AM."])
# Giving up on whose must never reach the when question - there is no list to put it on.
check_both("no whose, no when asked", [None, None, None],
           lands_on=None,
           asks=["Who's this reminder for? John or Jen?",
                 "I didn't get that. Was it John or Jen?",
                 "I didn't get that. Was it John or Jen?"])

print("\nPASS" if ok else "\nFAILED"); sys.exit(0 if ok else 1)
