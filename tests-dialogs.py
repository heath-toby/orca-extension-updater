#!/usr/bin/env python3
"""Extension Updater -- dialog tests.

Run after ./install.sh:

    ./tests-dialogs.py

**No window is ever shown.** Each dialog is constructed and inspected, and
then destroyed, without ``show_all``. That is deliberate: these tests run on
a live desktop, and a dialog that appeared would take the keyboard focus
away from whatever the user was reading. Constructing a GTK window maps
nothing, so the structure can be checked without interrupting anybody.

What cannot be checked this way is how the dialogs sound and navigate. That
is a field test, and the README says so.
"""

import os
import sys

import orca.extension_loader  # noqa: F401  (import order: see install.sh)

sys.path.insert(0, os.path.expanduser("~/.local/share/orca/extensions"))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from extension_updater import update_ui as U  # noqa: E402
from extension_updater.installer import Result  # noqa: E402
from extension_updater.inventory import Managed  # noqa: E402

rows, fails = [], 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    rows.append((name, repr(want), repr(got), "ok" if ok else "FAIL"))


def managed(source_id, label, version, repo):
    return Managed(source_id, label.replace(" ", ""), label, version, repo, "", True)


AVAILABLE = [
    U.Available(managed("polyglot", "Polyglot", "2.10.0", "heath-toby/polyglot-for-orca"), "v2.11.0"),
    U.Available(managed("clock", "Clock", "2.0.0", "heath-toby/Clock"), "v2.1.0"),
    U.Available(managed("capitalbeep", "CapitalBeep", "2.3.0", "heath-toby/orca-capitalbeep"), "v2.4.0"),
]

# --- What a row says ---------------------------------------------------------

check("a row names the add-on and both versions",
      AVAILABLE[0].row_label, "Polyglot 2.10.0 to 2.11.0")
check("a tag's leading v is not read out", AVAILABLE[0].to_version, "2.11.0")
check("a tag without a v is handled too",
      U.Available(AVAILABLE[0].item, "2.11.0").to_version, "2.11.0")


# --- The selection dialog ----------------------------------------------------

dialog = U.SelectionDialog(AVAILABLE)
try:
    check("the dialog names itself for the screen reader",
          dialog.get_accessible().get_name(), "Extension updates")

    checks = [
        w for w in dialog.get_content_area().get_children()
        if isinstance(w, Gtk.CheckButton)
    ]
    check("there is one tickbox per update", len(checks), 3)
    check("every tickbox is focusable", [c for c in checks if not c.get_can_focus()], [])
    check("every tickbox starts ticked", [c for c in checks if not c.get_active()], [])
    check("every tickbox has an accessible name",
          [c for c in checks if not c.get_accessible().get_name()], [])
    check("the first tickbox is named after its add-on",
          checks[0].get_accessible().get_name(), "Polyglot 2.10.0 to 2.11.0")

    labels = [w for w in dialog.get_content_area().get_children() if isinstance(w, Gtk.Label)]
    check("the heading counts the updates",
          labels[0].get_text(), "3 extensions have updates available.")

    # Update all ignores the tickboxes; Update selected honours them.
    checks[1].set_active(False)
    check("update all takes everything",
          [a.item.source_id for a in dialog.chosen(dialog.RESPONSE_ALL)],
          ["polyglot", "clock", "capitalbeep"])
    check("update selected takes only the ticked ones",
          [a.item.source_id for a in dialog.chosen(dialog.RESPONSE_SELECTED)],
          ["polyglot", "capitalbeep"])
    check("not now takes nothing",
          dialog.chosen(Gtk.ResponseType.CANCEL), [])
    for entry in checks:
        entry.set_active(False)
    check("nothing ticked means nothing to do",
          dialog.chosen(dialog.RESPONSE_SELECTED), [])
finally:
    dialog.destroy()

single = U.SelectionDialog(AVAILABLE[:1])
try:
    labels = [w for w in single.get_content_area().get_children() if isinstance(w, Gtk.Label)]
    check("one update is announced in the singular",
          labels[0].get_text(), "One extension has an update available.")
finally:
    single.destroy()


# --- The progress dialog -----------------------------------------------------

progress = U.ProgressDialog(3)
try:
    check("the progress dialog names itself",
          progress.get_accessible().get_name(), "Updating extensions")
    bars = [w for w in progress.get_content_area().get_children() if isinstance(w, Gtk.ProgressBar)]
    check("there is a real progress bar", len(bars), 1)
    bar = bars[0]
    check("the bar starts empty", bar.get_fraction(), 0.0)

    progress.begin("Polyglot")
    check("the bar names what it is updating",
          bar.get_accessible().get_name(), "Updating Polyglot")
    check("the bar's text counts the run", bar.get_text(), "Updating Polyglot (1 of 3)")

    progress.set_fraction(0.25)
    check("the bar follows the fraction", round(bar.get_fraction(), 3), 0.25)

    # A new extension restarts the sweep, rather than continuing the last one.
    progress.set_fraction(1.0)
    progress.begin("Clock")
    check("each extension starts a fresh sweep", bar.get_fraction(), 0.0)
    check("the bar renames itself per extension",
          bar.get_accessible().get_name(), "Updating Clock")
    check("the count advances", bar.get_text(), "Updating Clock (2 of 3)")

    # Out-of-range values are clamped, never wrapped: a bar that jumped back
    # to the start would sound like the update had restarted.
    progress.set_fraction(1.5)
    check("over-full is clamped to full", bar.get_fraction(), 1.0)
    progress.set_fraction(-0.5)
    check("negative is clamped to empty", bar.get_fraction(), 0.0)
finally:
    progress.destroy()


# --- The completion summary --------------------------------------------------

def summary(results):
    return " ".join(U.summarise(results))


ok_result = Result("Polyglot", "polyglot", "2.10.0", "2.11.0", ok=True)
ok_with_script = Result("Clock", "clock", "2.0.0", "2.1.0", ok=True, has_install_script=True)
failed = Result("CapitalBeep", "capitalbeep", "2.3.0", "2.4.0", ok=False,
                error="the download failed")
broken = Result("Clock", "clock", "2.0.0", "2.1.0", ok=False,
                error="the new version did not load", unrecoverable=True)

text = summary([ok_result])
check("a success names the add-on and version", "Polyglot to 2.11.0" in text, True)
check("a success says the update is already active", "already active" in text, True)
check("a success does not mention a failure", "not updated" in text, False)

text = summary([failed])
check("a failure names the add-on", "CapitalBeep was not updated" in text, True)
check("a failure gives the reason", "the download failed" in text, True)
check("a failure does not claim anything is active", "already active" in text, False)

text = summary([ok_result, failed])
check("a mixed run reports both", "Polyglot to 2.11.0" in text and "CapitalBeep" in text, True)

text = summary([broken])
check("an unrecoverable failure says to restart", "Restart Orca now" in text, True)
check("an unrecoverable failure names the recovery",
      "install.sh" in text, True)
check("an unrecoverable failure does not say things are active",
      "already active" in text, False)

text = summary([ok_with_script])
check("an add-on with its own install.sh is flagged",
      "dependency" in text, True)

check("an empty run says nothing was updated",
      summary([]), "Nothing was updated.")


width = max(len(row[0]) for row in rows)
print()
for name, want, got, status in rows:
    print(f"  {status:4s} {name:<{width}}  want={want}  got={got}")
print(f"\n{len(rows) - fails}/{len(rows)} rows green")
sys.exit(1 if fails else 0)
