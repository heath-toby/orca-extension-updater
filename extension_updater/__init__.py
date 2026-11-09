"""Extension Updater: tells you when your Orca add-ons have new releases.

Orca 51 user extension. Install to $XDG_DATA_HOME/orca/extensions/ and
approve with ``orca --approve-extension extension_updater``; Orca re-checks
the hash of every file in the package on each start, so re-approve after
editing.

It reads the metadata Orca already holds for every installed extension --
including ``WEBSITE``, which Orca parses with ``ast`` and never executes --
asks GitHub whether a newer release exists, and offers the updates in a
dialog with a tickbox each. Nothing is ever updated without being ticked.
"""

from __future__ import annotations

import os
import time

from orca.extension import Extension
from orca.extension_preferences import ExtensionPreference

# Import submodules as `from .module import name`, never as
# `from . import module`. The loader imports us as
# orca_user_extension.<name> but never creates `orca_user_extension`
# itself, and `from . import module` goes through __import__ with the
# full dotted path, which insists on importing that absent grandparent.
from .inventory import discover as _discover
from .inventory import extensions_dir as _extensions_dir
from .inventory import is_newer as _is_newer
from .scheduler import INTERVAL_DEFAULT as _INTERVAL_DEFAULT
from .scheduler import INTERVALS as _INTERVALS
from .scheduler import Scheduler as _Scheduler

__version__ = "1.0.0"

# Where the log goes. Never inside the package: Orca hashes every file in an
# extension's directory, so a log written there would un-approve the add-on
# the first time it was appended to.
_LOG_NAME = "debug.log"
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_STAMP = "%Y-%m-%d %H:%M:%S"

_updater: "ExtensionUpdater | None" = None


def _check_now() -> bool:
    """Command handler for Orca+Shift+U."""

    from gi.repository import GLib

    if _updater is not None:
        GLib.idle_add(_updater.run_check, True)
    return True


class ExtensionUpdater(Extension):
    """Checks GitHub for new releases of the installed extensions."""

    GROUP_LABEL = "Extension Updater"
    DESCRIPTION = (
        "Checks whether the installed Orca extensions have newer releases on "
        "GitHub, and offers to install the ones you tick. Each update is "
        "checked before it replaces anything, and the previous version is "
        "restored if it does not load."
    )
    VERSION = "1.0.0"  # keep in sync with __version__; read by AST, must be a literal
    AUTHOR = "Toby"
    WEBSITE = "https://github.com/heath-toby/orca-extension-updater"

    def __init__(self) -> None:
        super().__init__()
        self._scheduler: _Scheduler | None = None
        self._flow = None
        self._client = None
        self._checking = False
        global _updater
        _updater = self

    # --- Preferences -----------------------------------------------------

    def get_preferences(self) -> list[ExtensionPreference]:
        """Returns this extension's declarative preferences.

        Only the three settings Orca's generated dialog renders well. The
        update dialog itself is hand-written because it has to be: Orca's
        declarative kinds have no multi-select, so "tick the ones you want"
        cannot be expressed as a preference.
        """

        return [
            ExtensionPreference.enum(
                "interval",
                "How often to check for extension updates",
                tuple((value, label) for value, label, _seconds in _INTERVALS),
                _INTERVAL_DEFAULT,
            ),
            ExtensionPreference.boolean(
                "check-at-startup", "Also check shortly after Orca starts", True
            ),
            ExtensionPreference.boolean(
                "debug", "Log diagnostics to orca/extension-updater/debug.log", False
            ),
        ]

    # --- Commands --------------------------------------------------------

    def _get_commands(self) -> list:
        """Returns this extension's keyboard commands."""

        from orca import command_manager, keybindings

        keybinding = keybindings.KeyBinding(
            "u", keybindings.ORCA_SHIFT_MODIFIER_MASK
        )
        return [
            command_manager.KeyboardCommand(
                name="extensionUpdaterCheck",
                function=_check_now,
                group_label=self.GROUP_LABEL,
                description="Check for extension updates now",
                desktop_keybinding=keybinding,
                laptop_keybinding=keybinding,
            )
        ]

    # --- Lifecycle -------------------------------------------------------

    def on_ready(self) -> None:
        """Starts the check schedule once Orca is up."""

        self._start()

    def on_enabled(self) -> None:
        """Starts the schedule again after a reload."""

        self._start()

    def on_disabled(self) -> None:
        """Stops the schedule and abandons anything in flight."""

        self._stop()

    def on_shutdown(self) -> None:
        """Deliberately does nothing.

        Orca runs shutdown hooks in a daemon thread with a budget of half a
        second across every extension, and tearing this one down means GLib
        source removal on the main thread. Doing it off-thread risks a
        warning or a hung exit and buys nothing: the process is going away.
        """

    def _start(self) -> None:
        if self._scheduler is None:
            self._scheduler = _Scheduler(
                interval=lambda: str(self.settings.get("interval", _INTERVAL_DEFAULT)),
                last_check=self._last_check,
                record_check=self._record_check,
                on_check=lambda: self.run_check(False),
                check_at_startup=lambda: bool(
                    self.settings.get("check-at-startup", True)
                ),
            )
        self._scheduler.start()
        self.log(f"started; interval={self.settings.get('interval', _INTERVAL_DEFAULT)}")

    def _stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._client is not None:
            self._client.cancel()
            self._client = None
        self._checking = False

    # --- Remembered state ------------------------------------------------

    def _last_check(self) -> int:
        try:
            return int(self.settings.get("last-check", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _record_check(self, when: int) -> None:
        self.settings.set("last-check", int(when))

    def _sizes(self) -> dict:
        value = self.settings.get("tarball-sizes", {})
        return value if isinstance(value, dict) else {}

    def _expected_size(self, repo: str) -> int:
        try:
            return int(self._sizes().get(repo, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _remember_size(self, repo: str, size: int) -> None:
        """Remembers a tarball's size, so next time the bar is accurate.

        GitHub does not send a Content-Length for a tag it has not served
        before, which is exactly the case for a new release, so the first
        download of any release is measured against this remembered figure.
        """

        if size <= 0:
            return
        sizes = dict(self._sizes())
        sizes[repo] = int(size)
        self.settings.set("tarball-sizes", sizes)

    # --- Logging ---------------------------------------------------------

    def log(self, message: str) -> None:
        """Appends a dated line to the log, when logging is on.

        Dated, not time-only. A time-only stamp in an append-only file that
        spans days makes any "what happened at 19:37" question match the
        wrong day, which has cost this project a wrong diagnosis before.
        """

        if not self.settings.get("debug", False):
            return
        from .installer import data_dir

        try:
            directory = data_dir()
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, _LOG_NAME)
            if os.path.exists(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
                os.replace(path, path + ".1")
            stamp = time.strftime(_LOG_STAMP)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {message}\n")
        except OSError:
            pass

    # --- The check -------------------------------------------------------

    def run_check(self, spoken: bool) -> bool:
        """Checks every managed extension for a newer release.

        `spoken` is True when the user asked by keystroke, in which case the
        answer is always said out loud -- a key that silently does nothing
        leaves you unable to tell a working check from a broken one.
        """

        from .github import Client
        from .update_ui import Available, Flow, speak

        if self._checking:
            if spoken:
                speak("Already checking for extension updates.")
            return False

        if self._flow is not None and self._flow.busy:
            if spoken:
                speak("An extension update is already in progress.")
            return False

        directory = _extensions_dir()
        try:
            managed, unmanaged = _discover(directory)
        except Exception as error:  # pylint: disable=broad-exception-caught
            self.log(f"could not read the installed extensions: {error}")
            if spoken:
                speak("Could not read the installed extensions.")
            return False

        self.log(f"checking {len(managed)} extensions; {len(unmanaged)} unmanaged")
        for entry in unmanaged:
            self.log(f"unmanaged: {entry.source_id} ({entry.reason})")

        if not managed:
            if spoken:
                speak(
                    "No installed extension says where it came from, so there is "
                    "nothing to check."
                )
            return False

        self._checking = True
        self._client = Client()
        pending = list(managed)
        available: list[Available] = []
        problems: list[str] = []

        def finished() -> None:
            self._checking = False
            self._client = None
            if self._scheduler is not None:
                self._scheduler.note_check_completed()
            for problem in problems:
                self.log(problem)

            if not available:
                self.log("no updates available")
                if spoken:
                    speak(
                        "No extension updates available."
                        if not problems
                        else f"No extension updates available. {len(problems)} "
                        "could not be checked."
                    )
                return

            self.log(
                "updates available: "
                + ", ".join(f"{a.item.label} {a.to_version}" for a in available)
            )
            if self._flow is None:
                self._flow = Flow(
                    directory,
                    log=self.log,
                    remember_size=self._remember_size,
                    expected_size=self._expected_size,
                )
            self._flow.offer(available)

        def step() -> None:
            if not pending:
                finished()
                return
            item = pending.pop(0)

            def got(tag: str | None, error: str | None) -> None:
                if error is not None:
                    problems.append(f"{item.label}: {error}")
                elif tag and _is_newer(tag, item.version):
                    available.append(Available(item, tag))
                step()

            assert self._client is not None
            self._client.latest_version(item.repo, got)

        step()
        return False
