"""The chime picker: two built-in sounds, plus whatever is in <config>/media/Chimes.

`python3 tests/test_chime_folder.py`. Stubs the Home Assistant modules config_flow imports.

The folder is optional and a user's, so the cases that matter are the ones where it is
absent, empty, or full of things that are not sounds - none of which may break the form.
"""
import sys, types, importlib.util, tempfile, pathlib

for name in ["homeassistant","homeassistant.config_entries","homeassistant.core",
             "homeassistant.helpers","homeassistant.helpers.selector",
             "homeassistant.data_entry_flow","voluptuous"]:
    sys.modules.setdefault(name, types.ModuleType(name))
ce=sys.modules["homeassistant.config_entries"]
class _Flow:
    def __init_subclass__(cls, **kw): pass
ce.ConfigEntry=object; ce.ConfigFlow=_Flow; ce.OptionsFlow=_Flow
sys.modules["homeassistant.core"].callback=lambda f: f
sys.modules["homeassistant.data_entry_flow"].section=lambda *a, **k: None
sel=sys.modules["homeassistant.helpers.selector"]
sel.SelectOptionDict=lambda value, label: {"value": value, "label": label}
for n in ("TextSelector","EntitySelector","EntitySelectorConfig","TimeSelector",
          "BooleanSelector","SelectSelector","SelectSelectorConfig","SelectSelectorMode",
          "MediaSelector","MediaSelectorConfig"):
    setattr(sel, n, type(n, (), {"__init__": lambda self,*a,**k: None}))
sys.modules["homeassistant.helpers"].selector=sel
vol=sys.modules["voluptuous"]
vol.Schema=lambda *a, **k: None
vol.Required=lambda *a, **k: None
vol.Optional=lambda *a, **k: None

import os, pathlib
# Set FGR_SRC to test a copy somewhere else - a live /config, say. Otherwise this
# checkout is what gets tested.
SRC = pathlib.Path(os.environ.get("FGR_SRC") or
                   pathlib.Path(__file__).resolve().parent.parent
                   / "custom_components" / "fiendishly_reminders")
pkg=types.ModuleType("fr"); pkg.__path__=[str(SRC)]
sys.modules["fr"]=pkg
for m in ("const","config_flow"):
    sp=importlib.util.spec_from_file_location("fr."+m, SRC / f"{m}.py")
    mod=importlib.util.module_from_spec(sp); sys.modules["fr."+m]=mod; sp.loader.exec_module(mod)
cf=sys.modules["fr.config_flow"]

ok=True
def check(label, got, want):
    global ok
    if got != want:
        ok=False; print(f"FAIL {label}\n      got  {got}\n      want {want}")
    else:
        print(f"ok   {label}")

BUILT_IN=["fgReminders - Fiendishly Good Reminders default chime",
          "Home Assistant's own chime"]
def labels(opts): return [o["label"] for o in opts]

# The two built-ins are always there - the folder only ever ADDS.
check("no folder given", labels(cf._list_chimes(None)), BUILT_IN)
with tempfile.TemporaryDirectory() as d:
    chimes = pathlib.Path(d) / "Chimes"
    check("folder does not exist", labels(cf._list_chimes(str(chimes))), BUILT_IN)
    chimes.mkdir()
    check("folder empty", labels(cf._list_chimes(str(chimes))), BUILT_IN)

    for n in ("Test Bell.mp3", "soft ping.wav", "Zebra.ogg", "notes.txt", "cover.jpg"):
        (chimes / n).write_bytes(b"x")
    (chimes / "subfolder").mkdir()
    opts = cf._list_chimes(str(chimes))
    # Sounds only, sorted, named by their stem - a filename is the only name there is.
    check("three sounds, alphabetical", labels(opts),
          BUILT_IN + ["Test Bell", "Zebra", "soft ping"])
    check("non-audio ignored", [o for o in opts if "notes" in o["value"] or "cover" in o["value"]], [])
    check("a folder is not a chime", [o for o in opts if "subfolder" in o["value"]], [])
    # The value is the URL the integration publishes the file at, url-quoted: media_source
    # cannot see inside the config directory, so a media-source id would not resolve.
    check("value is the served URL", opts[2]["value"],
          "/fiendishly_reminders/chimes/Test%20Bell.mp3")

    # A stored sound whose file has gone must not be handed back as the form's default -
    # a SelectSelector rejects a default it does not offer, and the form would not open.
    check("missing file falls back", cf._chime_default({"chime": "/fiendishly_reminders/chimes/gone.mp3"}, opts), "fg")
    check("stored sound kept", cf._chime_default({"chime": opts[2]["value"]}, opts), opts[2]["value"])

print("\nPASS" if ok else "\nFAILED"); sys.exit(0 if ok else 1)
