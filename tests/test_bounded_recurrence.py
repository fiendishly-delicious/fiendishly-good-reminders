"""Bounded recurrence - UNTIL normalisation and the prose that describes a bound.

`python3 tests/test_bounded_recurrence.py`. Stubs the few Home Assistant modules
store.py imports, so it runs in about a second with no HA.

The UNTIL cases exist because an RFC5545 "...Z" bound was rejected outright: occurrences
are expanded against a NAIVE local dtstart (deliberately - it is what keeps 9am at 9am
across DST) and dateutil will not mix that with a timezone-aware UNTIL.
"""
import sys, types, datetime, importlib.util
for name in ["homeassistant","homeassistant.core","homeassistant.helpers",
             "homeassistant.helpers.storage","homeassistant.util","homeassistant.util.dt"]:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.core"].HomeAssistant=object
sys.modules["homeassistant.helpers.storage"].Store=object
LOCAL=datetime.timezone(datetime.timedelta(hours=-7))
dt=sys.modules["homeassistant.util.dt"]
dt.DEFAULT_TIME_ZONE=LOCAL
dt.parse_datetime=lambda v: None
dt.now=lambda: datetime.datetime.now(LOCAL)
dt.as_local=lambda d: d.astimezone(LOCAL) if d.tzinfo else d
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

ok=True
def check(label, got, want):
    global ok
    good = got==want; ok = ok and good
    print(f" {'ok ' if good else 'FAIL'} {label:44} -> {got!r}")
    if not good: print(f"      wanted {want!r}")

print("=== normalize_rrule: UNTIL becomes naive local ===")
check("UTC Z bound", st.normalize_rrule("FREQ=WEEKLY;BYDAY=SU;UNTIL=20261018T040000Z"),
      "FREQ=WEEKLY;BYDAY=SU;UNTIL=20261017T210000")
check("date-only bound covers that day",
      st.normalize_rrule("FREQ=DAILY;UNTIL=20261018"), "FREQ=DAILY;UNTIL=20261018T235959")
check("already-naive bound untouched",
      st.normalize_rrule("FREQ=DAILY;UNTIL=20261018T235959"), "FREQ=DAILY;UNTIL=20261018T235959")
check("COUNT untouched", st.normalize_rrule("FREQ=WEEKLY;BYDAY=SU;COUNT=6"),
      "FREQ=WEEKLY;BYDAY=SU;COUNT=6")

print("=== describe_rrule: the bound is in the prose ===")
check("count", st.describe_rrule("FREQ=WEEKLY;BYDAY=SU;COUNT=6"), "every Sunday, 6 times")
check("count of one", st.describe_rrule("FREQ=DAILY;COUNT=1"), "every day, 1 time")
check("until", st.describe_rrule("FREQ=WEEKLY;BYDAY=SU;UNTIL=20261018T235959"),
      "every Sunday, until October 18")
check("unbounded unchanged", st.describe_rrule("FREQ=WEEKLY;BYDAY=TU"), "every Tuesday")
check("monthly with bound", st.describe_rrule("FREQ=MONTHLY;BYMONTHDAY=1;COUNT=3"),
      "on the 1st of every month, 3 times")
check("weekdays with until", st.describe_rrule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20261225T235959"),
      "every weekday, until December 25")

print("=== a bounded series actually expands and then stops ===")
base=datetime.datetime(2026,9,6,21,0,tzinfo=LOCAL)
r=st.Reminder(id="x", text="t", due=base, dtstart=base, rrule="FREQ=WEEKLY;BYDAY=SU;COUNT=6")
seen=[base]; cur=base
for _ in range(10):
    n=r.next_occurrence(cur)
    if n is None: break
    seen.append(n); cur=n
check("COUNT=6 yields six occurrences", len(seen), 6)
check("last is Oct 11", seen[-1].strftime("%Y-%m-%d"), "2026-10-11")

r2=st.Reminder(id="y", text="t", due=base, dtstart=base, rrule="FREQ=WEEKLY;BYDAY=SU;UNTIL=20261017T235959")
seen2=[base]; cur=base
for _ in range(10):
    n=r2.next_occurrence(cur)
    if n is None: break
    seen2.append(n); cur=n
check("UNTIL Oct 17 yields six occurrences", len(seen2), 6)
check("last is Oct 11", seen2[-1].strftime("%Y-%m-%d"), "2026-10-11")
print("ALL PASS" if ok else "*** SOME FAILED ***")
