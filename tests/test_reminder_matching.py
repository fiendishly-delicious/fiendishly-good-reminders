"""Reminder text matching, exercised without a running Home Assistant.

`python3 tests/test_reminder_matching.py` - stubs the handful of HA modules store.py
imports so the matching tiers can be run against a fake store in about a second.

It exists because every case in here came from a real failure. Someone said "cancel my
walk Ozzy reminder" to a Voice PE four different ways and was told each time that no
such reminder existed, while "list my reminders" read it straight back out. The pipeline
transcripts showed why: the recognizer had heard "walk Aussie", "walk but with Ozzy" and
"walk Ozzy tomorrow morning" - none of which contain the stored text as a substring.

Lives here rather than on the Yellow because Home Assistant never reads it.
"""
import sys, types, datetime, importlib.util
for name in ["homeassistant","homeassistant.core","homeassistant.helpers",
             "homeassistant.helpers.storage","homeassistant.util","homeassistant.util.dt",
             "dateutil","dateutil.rrule"]:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.core"].HomeAssistant=object
sys.modules["homeassistant.helpers.storage"].Store=object
sys.modules["dateutil.rrule"].rrulestr=lambda *a,**k: None
dt=sys.modules["homeassistant.util.dt"]
dt.DEFAULT_TIME_ZONE=datetime.timezone.utc; dt.parse_datetime=lambda v: None
dt.now=lambda: datetime.datetime.now(datetime.timezone.utc); dt.as_local=lambda d: d
import os, pathlib
# Set FGR_SRC to test a copy somewhere else - a live /config, say. Otherwise this
# checkout is what gets tested.
SRC = pathlib.Path(os.environ.get("FGR_SRC") or
                   pathlib.Path(__file__).resolve().parent.parent
                   / "custom_components" / "fiendishly_reminders")
pkg=types.ModuleType("fr"); pkg.__path__=[str(SRC)]
sys.modules["fr"]=pkg
for m in ("const","store"):
    sp=importlib.util.spec_from_file_location("fr."+m, SRC / f"{m}.py")
    mod=importlib.util.module_from_spec(sp); sys.modules["fr."+m]=mod; sp.loader.exec_module(mod)
st=sys.modules["fr.store"]
base=datetime.datetime(2026,9,6,7,15,tzinfo=datetime.timezone.utc)
class S(st.ReminderStore):
    def __init__(self, texts):
        self.reminders={str(i):st.Reminder(id=str(i),text=t,due=base+datetime.timedelta(hours=i),dtstart=base) for i,t in enumerate(texts)}
    def async_save(self): pass
s=S(["make dinner for everybody uh and myself","walk Ozzy","look busy","take the trash out"])
ok=True
def check(kind, fn, needle, want):
    global ok
    got=[r.text for r in fn(needle)]; good=got==want; ok=ok and good
    print(f" {'ok ' if good else 'FAIL'} {kind} {needle!r:32} -> {got}")
print("=== find (strict) ===")
for n,w in [("walk Ozzy",["walk Ozzy"]),("walk but with Ozzy",["walk Ozzy"]),
            ("walk, Ozzy",["walk Ozzy"]),("walk Ozzy tomorrow morning",["walk Ozzy"]),
            ("look busy",["look busy"]),("trash",["take the trash out"]),
            ("feed the llama",[]),("dinner",["make dinner for everybody uh and myself"]),
            ("Walk OZZY",["walk Ozzy"]),("the",[]),("a",[]),("my the",[])]:
    check("find      ", s.find, n, w)
print("=== find_loose (confirmation names the guess) ===")
for n,w in [("walk Aussie",["walk Ozzy"]),("walk Ozzie",["walk Ozzy"]),
            ("walk Aussie tomorrow",["walk Ozzy"]),("busy",["look busy"]),
            ("feed the llama",[]),("the",[]),("a",[]),("cancel it",[])]:
    check("find_loose", s.find_loose, n, w)

# The case that destroyed a real reminder: two candidates tie on the word "walk".
print("=== find_loose with a competing 'walk' reminder ===")
s2=S(["walk Ozzy","walk Rufus","look busy","take the trash out"])
for n,w in [("walk Roofus",["walk Rufus"]),
            # Two "walk X" reminders and a mangled X: no honest way to choose.
            ("walk Aussie",[]),
            # Exact substring of both - that is tier 1, not a guess, and the
            # confirmation says "2 reminders".
            ("walk",["walk Ozzy","walk Rufus"]),
            ("walk someone",[])]:
    check("find_loose", s2.find_loose, n, w)

# Genuinely indistinguishable: refuse rather than pick.
print("=== find_loose, truly ambiguous ===")
s3=S(["walk Ozzy","walk Ozzie"])
# "walk ozzi" is an exact substring of "walk ozzie" and of nothing else: tier 1.
for n,w in [("walk Ozzi",["walk Ozzie"]),("walk Ozzy",["walk Ozzy"])]:
    check("find_loose", s3.find_loose, n, w)
print("ALL PASS" if ok else "*** SOME FAILED ***")
