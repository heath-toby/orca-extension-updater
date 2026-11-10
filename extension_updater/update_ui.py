"""The three dialogs, and the sequence that drives them.

House style for an Orca add-on's own GTK dialog, taken from Clock and
Polyglot: GTK 3, AT-SPI events suspended around building and tearing down the
window, the suspension lifted on the first focus grab rather than on
``show_all``, and the window opened from the main loop rather than from inside
a keybinding handler.

Nothing here uses ``Gtk.Dialog.run()``. That spins a nested main loop, and an
update is asynchronous from start to finish -- the download reports progress
through the main loop while a dialog is on screen. Signals only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Atk", "1.0")
from gi.repository import Atk, GLib, Gtk  # noqa: E402

from .installer import Installer, Result
from .inventory import Managed

_resume_timer_id: int | None = None

# Building a window from inside Orca floods Orca with its own AT-SPI events.
_EVENTS_TO_SUSPEND = (
    "object:state-changed:focused",
    "object:state-changed:showing",
    "object:children-changed:",
    "object:property-change:accessible-name",
)

_RESUME_DELAY_MS = 500


def _suspend_events() -> None:
    """Stops Orca listening to our own window being built.

    Cancels a pending resume first. Without that, a resume armed by the
    previous dialog fires in the middle of building this one -- which is the
    bug Clock fixed and Polyglot's older copy of this code still has.
    """

    global _resume_timer_id
    if _resume_timer_id is not None:
        GLib.source_remove(_resume_timer_id)
        _resume_timer_id = None
    try:
        from orca import event_manager

        manager = event_manager.get_manager()
        for event in _EVENTS_TO_SUSPEND:
            manager.deregister_listener(event)
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _resume_events() -> bool:
    global _resume_timer_id
    _resume_timer_id = None
    try:
        from orca import event_manager

        manager = event_manager.get_manager()
        for event in _EVENTS_TO_SUSPEND:
            manager.register_listener(event)
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    return False


def _schedule_resume() -> None:
    global _resume_timer_id
    if _resume_timer_id is not None:
        GLib.source_remove(_resume_timer_id)
    _resume_timer_id = GLib.timeout_add(_RESUME_DELAY_MS, _resume_events)


def speak(message: str) -> None:
    """Says something through Orca, if Orca is there to say it."""

    try:
        from orca import presentation_manager

        presentation_manager.get_manager().present_message(message)
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _name(widget: Gtk.Widget, name: str, description: str = "") -> None:
    """Gives a widget an accessible name, and optionally a description."""

    accessible = widget.get_accessible()
    if accessible is None:
        return
    accessible.set_name(name)
    if description:
        accessible.set_description(description)


# --- Available updates -------------------------------------------------------


class Available:
    """One extension with an update waiting."""

    def __init__(self, item: Managed, tag: str) -> None:
        self.item = item
        self.tag = tag

    @property
    def to_version(self) -> str:
        """Returns the new version without its tag's leading v."""

        return self.tag.lstrip("v")

    @property
    def row_label(self) -> str:
        """Returns the tickbox label: what it is, and from where to where."""

        return f"{self.item.label} {self.item.version} to {self.to_version}"


class SelectionDialog(Gtk.Dialog):
    """A tickbox per available update, with Update selected and Update all."""

    RESPONSE_SELECTED = 1
    RESPONSE_ALL = 2

    def __init__(self, available: list[Available]) -> None:
        super().__init__(title="Extension updates", modal=True)
        _name(self, "Extension updates")
        self.set_default_size(460, -1)

        self._checks: list[tuple[Available, Gtk.CheckButton]] = []

        count = len(available)
        heading = Gtk.Label(
            label=(
                "One extension has an update available."
                if count == 1
                else f"{count} extensions have updates available."
            )
        )
        heading.set_xalign(0.0)
        heading.set_line_wrap(True)

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)
        content.pack_start(heading, False, False, 0)

        first: Gtk.CheckButton | None = None
        for entry in available:
            check = Gtk.CheckButton(label=entry.row_label)
            check.set_active(True)
            check.set_can_focus(True)
            _name(check, entry.row_label, f"Update {entry.item.label}")
            accessible = check.get_accessible()
            if accessible is not None:
                accessible.set_role(Atk.Role.CHECK_BOX)
            content.pack_start(check, False, False, 0)
            self._checks.append((entry, check))
            if first is None:
                first = check
        self._first = first

        self.add_button("Not now", Gtk.ResponseType.CANCEL)
        update_selected = self.add_button("Update selected", self.RESPONSE_SELECTED)
        update_all = self.add_button("Update all", self.RESPONSE_ALL)
        _name(update_selected, "Update selected")
        _name(update_all, "Update all")
        self.set_default_response(self.RESPONSE_SELECTED)

    def chosen(self, response: int) -> list[Available]:
        """Returns the updates the response asks for."""

        if response == self.RESPONSE_ALL:
            return [entry for entry, _check in self._checks]
        if response == self.RESPONSE_SELECTED:
            return [entry for entry, check in self._checks if check.get_active()]
        return []

    def focus_first(self) -> None:
        """Puts focus on the first tickbox and lets Orca start listening again."""

        if self._first is not None:
            self._first.grab_focus()
        _schedule_resume()


# --- Progress ----------------------------------------------------------------


class ProgressDialog(Gtk.Dialog):
    """One progress bar, driven 0 to 100 per extension and then reset.

    The bar is a real ``Gtk.ProgressBar`` on purpose. Orca presents other
    applications' progress bars itself, so a real widget is heard the way
    every other progress bar on the system is heard, with no sound code of
    our own and nothing to go wrong on a Bluetooth sink. The beeps come from
    Orca's own beepProgressBarUpdates setting; with it off the bar is still
    readable, just silent.
    """

    def __init__(self, total: int) -> None:
        super().__init__(title="Updating extensions", modal=True)
        _name(self, "Updating extensions")
        self.set_default_size(420, -1)
        self.set_deletable(False)

        self._total = total
        self._index = 0

        self._status = Gtk.Label(label="Starting.")
        self._status.set_xalign(0.0)
        self._status.set_line_wrap(True)

        self._bar = Gtk.ProgressBar()
        self._bar.set_show_text(True)
        self._bar.set_fraction(0.0)
        _name(self._bar, "Update progress")

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)
        content.pack_start(self._status, False, False, 0)
        content.pack_start(self._bar, False, False, 0)

    def begin(self, label: str) -> None:
        """Starts a fresh sweep for one extension."""

        self._index += 1
        text = f"Updating {label} ({self._index} of {self._total})"
        self._status.set_text(text)
        self._bar.set_fraction(0.0)
        self._bar.set_text(text)
        # The bar names what it is measuring, so tabbing to it answers
        # "which one is this" without waiting for the next announcement.
        _name(self._bar, f"Updating {label}")

    def set_fraction(self, fraction: float) -> None:
        """Moves the bar. Values outside 0-1 are clamped, never wrapped."""

        self._bar.set_fraction(min(1.0, max(0.0, fraction)))

    def focus_first(self) -> None:
        """Focuses the bar and lets Orca start listening again."""

        self._bar.grab_focus()
        _schedule_resume()


# --- Completion --------------------------------------------------------------


class CompletionDialog(Gtk.Dialog):
    """What happened, and whether to restart now."""

    RESPONSE_RESTART = 1

    def __init__(
        self, results: list[Result], not_attempted: Sequence[str] = ()
    ) -> None:
        super().__init__(title="Updates complete", modal=True)
        _name(self, "Updates complete")
        self.set_default_size(480, -1)

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)

        for line in summarise(results, not_attempted):
            label = Gtk.Label(label=line)
            label.set_xalign(0.0)
            label.set_line_wrap(True)
            label.set_selectable(True)
            content.pack_start(label, False, False, 0)

        later = self.add_button("Restart later", Gtk.ResponseType.CLOSE)
        restart = self.add_button("Restart Orca now", self.RESPONSE_RESTART)
        _name(later, "Restart later")
        _name(restart, "Restart Orca now")
        self.set_default_response(Gtk.ResponseType.CLOSE)
        self._first = restart

    def focus_first(self) -> None:
        """Focuses the restart button and lets Orca start listening again."""

        self._first.grab_focus()
        _schedule_resume()


def summarise(results: list[Result], not_attempted: Sequence[str] = ()) -> list[str]:
    """Builds the lines the completion dialog shows and Orca reads."""

    updated = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    lines: list[str] = []

    if updated:
        lines.append(
            "Updated: "
            + ", ".join(f"{r.label} to {r.to_version}" for r in updated)
            + "."
        )
    for result in failed:
        lines.append(f"{result.label} was not updated: {result.error}.")

    if any(r.unrecoverable for r in results):
        lines.append(
            "One add-on could not be put back as it was. Restart Orca now, and if "
            "it does not come up, re-run that add-on's install.sh from its "
            "repository."
        )
    elif updated:
        # Each updated extension was reloaded and proved to load, so the work
        # is already live -- with one exception, which has to be said rather
        # than glossed over, because otherwise "already active" would be a
        # claim about the very add-on it is not true of.
        pending = [r for r in updated if r.restart_required]
        if pending:
            names = ", ".join(r.label for r in pending)
            live = [r for r in updated if not r.restart_required]
            if live:
                lines.append(
                    f"{', '.join(r.label for r in live)} is already active; it was "
                    "reloaded and checked."
                    if len(live) == 1
                    else f"{', '.join(r.label for r in live)} are already active; "
                    "each was reloaded and checked."
                )
            lines.append(
                f"{names} cannot reload itself, so restart Orca to run the new "
                "version. It is installed and approved already."
            )
        else:
            lines.append(
                "These updates are already active; each one was reloaded and "
                "checked. A restart is only needed for the updater itself, and is "
                "worth doing anyway after several add-ons have changed."
            )

    # A run cut short by the hourly allowance is not a fault to investigate,
    # it is a wait. Say which it was, because "GitHub refused the request"
    # sends the user looking for a problem that will fix itself.
    if not_attempted:
        lines.append(
            f"Not attempted: {', '.join(not_attempted)}."
            if len(not_attempted) <= 4
            else f"{len(not_attempted)} others were not attempted."
        )

    if any("hourly limit" in (r.error or "") for r in failed):
        lines.append(
            "That is a wait, not a fault: press Orca+Shift+U again once the limit "
            "resets and the rest will install. The versions already found are "
            "remembered, so a second attempt spends its requests on downloads "
            "rather than on looking again."
        )

    stranded = [r for r in updated if r.not_loaded]
    if stranded:
        names = ", ".join(r.label for r in stranded)
        plural = "" if len(stranded) == 1 else "s"
        lines.append(
            f"{names} installed, but Orca does not load it: and it was not loading "
            f"before the update either, so the update is not the cause. Check that "
            f"add-on{plural} against this version of Orca."
        )

    if any(r.ok and r.has_install_script for r in updated):
        lines.append(
            "If an updated add-on has gained a new dependency, run its own "
            "install.sh from a clone; this updater installs code, not "
            "dependencies."
        )

    if not results:
        lines.append("Nothing was updated.")
    return lines


# --- What a check found ------------------------------------------------------

# The reason an extension's repository publishes nothing to compare against.
# This is a settled fact about that repository, not a failure of the check.
NO_RELEASES = "no releases or version tags"


def names(labels: Sequence[str]) -> str:
    """Joins add-on names the way they would be read aloud."""

    labels = list(labels)
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def describe_check(
    unpublished: Sequence[str],
    failures: Sequence[tuple[str, str]],
    rate_limited: str = "",
) -> str:
    """Returns the sentence spoken when a check finds no updates.

    Three kinds of silence need telling apart, because they call for
    different things from the user:

    * nothing to report, which needs one short sentence;
    * a repository that publishes no releases at all, which is permanent and
      needs naming once so it can be acted on -- counting it, as this used to,
      says there is a problem without saying where, every single time;
    * a check that genuinely failed, which may be worth retrying.
    """

    if rate_limited:
        # Not "no updates available": the truth is that we could not find out.
        return f"Could not check for updates: {rate_limited}."

    parts = ["No extension updates available."]
    if failures:
        if len(failures) == 1:
            label, reason = failures[0]
            parts.append(f"{label} could not be checked: {reason}.")
        else:
            parts.append(
                f"{names([label for label, _reason in failures])} could not be checked."
            )
    if unpublished:
        verb = "publishes" if len(unpublished) == 1 else "publish"
        parts.append(
            f"{names(unpublished)} {verb} no releases, so there is nothing to check."
        )
    return " ".join(parts)


# --- Restarting --------------------------------------------------------------


def restart_orca() -> str:
    """Restarts Orca the way this Orca was started. Returns what was done.

    Getting this wrong kills the screen reader, so it is decided rather than
    assumed. Under systemd, ``orca.service`` carries ``Restart=always``, so
    the service manager owns the restart and ``orca --replace`` would fight
    it. Started as an ordinary application, ``orca --replace`` is right, but
    it SIGKILLs every Orca it finds -- so the replacement must be detached
    into its own session, or the command kills the very process meant to
    carry it out.
    """

    managed = False
    try:
        from orca import systemd

        managed = bool(systemd.get_manager().is_systemd_managed())
    except Exception:  # pylint: disable=broad-exception-caught
        managed = False

    if managed:
        systemctl = shutil.which("systemctl")
        if systemctl:
            subprocess.Popen(  # noqa: S603
                [systemctl, "--user", "restart", "orca"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "restarting through systemd"

    orca_binary = shutil.which("orca")
    if not orca_binary:
        return "could not find the orca command"
    subprocess.Popen(  # noqa: S603
        [orca_binary, "--replace"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    return "restarting with orca --replace"


# --- The flow ----------------------------------------------------------------


class Flow:
    """Shows the dialogs and runs the updates, one extension at a time."""

    def __init__(
        self,
        extensions_dir: str,
        log: Callable[[str], None] | None = None,
        remember_size: Callable[[str, int], None] | None = None,
        expected_size: Callable[[str], int] | None = None,
        self_source_id: str = "",
    ) -> None:
        self._extensions_dir = extensions_dir
        self._self_source_id = self_source_id
        self._log = log or (lambda _message: None)
        self._remember_size = remember_size or (lambda _repo, _size: None)
        self._expected_size = expected_size or (lambda _repo: 0)
        self._installer: Installer | None = None
        self._progress: ProgressDialog | None = None
        self._queue: list[Available] = []
        self._results: list[Result] = []
        self._not_attempted: list[str] = []
        self._busy = False

    @property
    def busy(self) -> bool:
        """Returns whether an update run is already under way."""

        return self._busy

    def offer(self, available: list[Available]) -> None:
        """Opens the selection dialog. Call from the main loop."""

        if self._busy or not available:
            return
        _suspend_events()
        dialog = SelectionDialog(available)

        def responded(widget: SelectionDialog, response: int) -> None:
            chosen = widget.chosen(response)
            _suspend_events()
            widget.destroy()
            _schedule_resume()
            if chosen:
                self._start(chosen)

        dialog.connect("response", responded)
        dialog.show_all()
        dialog.focus_first()

    def _start(self, chosen: list[Available]) -> None:
        self._busy = True
        self._queue = list(chosen)
        self._results = []
        self._not_attempted = []
        self._installer = Installer(
            self._extensions_dir, self._log, self_source_id=self._self_source_id
        )

        _suspend_events()
        self._progress = ProgressDialog(len(self._queue))
        self._progress.show_all()
        self._progress.focus_first()
        self._next()

    def _next(self) -> None:
        if not self._queue:
            self._finish()
            return

        entry = self._queue.pop(0)
        progress = self._progress
        if progress is not None:
            progress.begin(entry.item.label)

        def on_progress(fraction: float) -> None:
            if self._progress is not None:
                self._progress.set_fraction(fraction)

        def on_done(result: Result) -> None:
            self._results.append(result)
            if result.ok:
                self._remember_size(entry.item.repo, result.downloaded_bytes)
                self._log(f"updated {entry.item.source_id} to {result.to_version}")
            else:
                self._log(f"failed {entry.item.source_id}: {result.error}")
            if result.unrecoverable:
                # Do not keep swapping packages under an Orca that is already
                # in a state we could not undo.
                self._queue = []
            elif "hourly limit" in (result.error or ""):
                # Every remaining download would be refused for the same
                # reason, so stop rather than produce a column of identical
                # failures. They are reported as not attempted, which is the
                # truth and is also what tells the user there is something
                # left to come back for.
                self._not_attempted = [entry.item.label for entry in self._queue]
                self._queue = []
                self._log(
                    f"stopped after the hourly limit; {len(self._not_attempted)} "
                    "not attempted"
                )
            self._next()

        assert self._installer is not None
        self._installer.update(
            entry.item,
            entry.tag,
            on_progress,
            on_done,
            self._expected_size(entry.item.repo),
        )

    def _finish(self) -> None:
        progress = self._progress
        self._progress = None
        self._installer = None
        self._busy = False

        _suspend_events()
        if progress is not None:
            progress.destroy()

        results = self._results
        dialog = CompletionDialog(results, self._not_attempted)

        def responded(widget: CompletionDialog, response: int) -> None:
            _suspend_events()
            widget.destroy()
            _schedule_resume()
            if response == CompletionDialog.RESPONSE_RESTART:
                speak(restart_orca())

        dialog.connect("response", responded)
        dialog.show_all()
        dialog.focus_first()
