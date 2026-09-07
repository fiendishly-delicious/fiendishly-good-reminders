"""Turning a reminder round to face the person it is read to.

`python3 tests/test_second_person.py`. Imports the function directly - it touches no
Home Assistant machinery at all.

You say "remind me to take my pills"; the house says it back to you, so it has to say
"take your pills". This runs at DELIVERY only. The store, the to-do list, the card and
every text match keep the words you actually used, because those are yours to read - and
because rewriting them would mean "cancel my take my pills reminder" no longer matched
what was stored.
"""
import os, sys, re, pathlib, importlib.util

# Set FGR_SRC to test a copy somewhere else - a live /config, say. Otherwise this
# checkout is what gets tested.
SRC = pathlib.Path(os.environ.get("FGR_SRC") or
                   pathlib.Path(__file__).resolve().parent.parent
                   / "custom_components" / "fiendishly_reminders")
# __init__.py imports half of Home Assistant, so pull the function out on its own.
src = (SRC / "__init__.py").read_text()
start = src.index("_SELF_WORDS = {")
end = src.index("def sole_manager(")
ns = {"re": re}
exec(src[start:end], ns)
second_person = ns["second_person"]

ok = True
def check(said, want):
    global ok
    got = second_person(said)
    if got != want:
        ok = False; print(f"FAIL {said!r}\n      got  {got!r}\n      want {want!r}")
    else:
        print(f"ok   {said!r}\n     -> {got!r}")

# the everyday cases
check("take my pills", "take your pills")
check("call my mother", "call your mother")
check("email Mike about my car", "email Mike about your car")
check("check that I locked the door", "check that you locked the door")
check("tell Bob I'm leaving", "tell Bob you're leaving")
check("see if I've paid it", "see if you've paid it")
check("remember I'll be late", "remember you'll be late")
check("do it myself", "do it yourself")
check("the blue one is mine", "the blue one is yours")

# case is kept where the word carried it
check("My pills", "Your pills")
check("I am late for the dentist", "You are late for the dentist")
check("call Ann. I am late", "call Ann. You are late")

# and the words that only LOOK self-referring
check("inspect the mine", "inspect the mine")
check("visit a mine", "visit a mine")
check("the coal mine tour", "the coal mine tour")
check("watch Lanterns", "watch Lanterns")
check("take the trash out", "take the trash out")
check("email Mimi about the timer", "email Mimi about the timer")
check("mind the gap", "mind the gap")
check("buy limes", "buy limes")
# a bare word must not be caught inside a longer one
check("my mimicry improves", "your mimicry improves")

print("\nPASS" if ok else "\nFAILED"); sys.exit(0 if ok else 1)
