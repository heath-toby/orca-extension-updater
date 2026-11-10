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

def summary(results, not_attempted=()):
    return " ".join(U.summarise(results, not_attempted))


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

# A run cut short by the hourly allowance: a wait, not a fault.
limited = Result("Wayland Clipboard", "wayland_clipboard", "1.2.0", "1.2.1", ok=False,
                 error="GitHub's hourly limit of 60 requests is used up; it resets in 35 minutes")
text = summary([ok_result, limited])
check("a rate-limited run still reports what succeeded",
      "Polyglot to 2.11.0" in text, True)
check("a rate-limited run names the add-on left behind",
      "Wayland Clipboard was not updated" in text, True)
check("a rate-limited run says when the limit resets",
      "35 minutes" in text, True)
check("a rate-limited run calls it a wait, not a fault",
      "a wait, not a fault" in text, True)
check("a rate-limited run says how to finish it",
      "press orca+shift+u again" in text.lower(), True)

# An ordinary failure must NOT get the retry-after-reset advice.
text = summary([failed])
check("an ordinary failure is not called a wait",
      "a wait, not a fault" in text, False)

# A run stopped by the allowance reports what it did not get to, which is
# what tells the user there is something to come back for.
text = summary([ok_result, limited], ["Audio Themes", "Clock"])
check("the not-attempted add-ons are named",
      "Not attempted: Audio Themes, Clock." in text, True)
check("the not-attempted line does not appear when there are none",
      "Not attempted" in summary([ok_result]), False)
check("a long not-attempted list is counted rather than listed",
      "6 others were not attempted." in summary(
          [limited], ["a", "b", "c", "d", "e", "f"]), True)
check("the retry advice mentions that versions are remembered",
      "remembered" in summary([limited], ["a"]), True)

# An add-on that installed but does not load, and was not loading before.
stranded = Result("Wayland Clipboard", "wayland_clipboard", "1.2.0", "1.2.1",
                  ok=True, loaded_before=False, not_loaded=True)
text = summary([stranded])
check("a stranded add-on is named", "Wayland Clipboard installed" in text, True)
check("a stranded add-on is not blamed on the update",
      "not the cause" in text, True)
check("a stranded add-on says it was already not loading",
      "not loading before the update" in text, True)
check("a stranded add-on is still counted as updated",
      "Updated: Wayland Clipboard to 1.2.1." in text, True)
check("an ordinary success is not called stranded",
      "does not load it" in summary([ok_result]), False)

text = summary([ok_with_script])
check("an add-on with its own install.sh is flagged",
      "dependency" in text, True)

check("an empty run says nothing was updated",
      summary([]), "Nothing was updated.")

# The updater updating itself: installed, but not live until a restart. The
# summary must not claim it is already active.
self_update = Result("Extension Updater", "extension_updater", "1.0.0", "1.0.1",
                     ok=True, restart_required=True)
text = summary([self_update])
check("a self-update says it cannot reload itself",
      "cannot reload itself" in text, True)
check("a self-update tells you to restart", "restart Orca" in text, True)
check("a self-update is not claimed to be active",
      "already active" in text, False)
check("a self-update says it is installed", "installed and approved" in text, True)

text = summary([ok_result, self_update])
check("a mixed self-update separates live from pending",
      "Polyglot is already active" in text and "cannot reload itself" in text, True)
check("a mixed self-update does not call everything active",
      "These updates are already active" in text, False)


width = max(len(row[0]) for row in rows)
print()
for name, want, got, status in rows:
    print(f"  {status:4s} {name:<{width}}  want={want}  got={got}")
print(f"\n{len(rows) - fails}/{len(rows)} rows green")
sys.exit(1 if fails else 0)
