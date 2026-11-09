"""When to check, and the timer that does it.

A one-shot ``GLib.timeout_add`` re-armed from its own callback, rather than a
repeating interval. Clock arrives at the same shape for the same reasons: the
callback is handed the deadline it was armed for instead of re-deriving "now"
(which skips boundaries when a timer fires late), and the live configuration
is re-checked before re-arming, so a timer belonging to a disabled extension
cannot restart it.

The last check is remembered on disk. Without that, a weekly check would
slide forward every time Orca restarted and in practice would never run.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from gi.repository import GLib

# (setting value, label, seconds). The labels are what the preferences
# dialog shows, so they are phrased as answers to "how often".
INTERVALS: tuple[tuple[str, str, int], ...] = (
    ("hourly", "Every hour", 3600),
    ("6-hourly", "Every six hours", 6 * 3600),
    ("12-hourly", "Every twelve hours", 12 * 3600),
    ("daily", "Every day", 24 * 3600),
    ("weekly", "Every week", 7 * 24 * 3600),
    ("monthly", "Every thirty days", 30 * 24 * 3600),
    ("off", "Never check automatically", 0),
)

INTERVAL_DEFAULT = "daily"

_BY_VALUE = {value: seconds for value, _label, seconds in INTERVALS}

# A check costs one HTTPS request per managed add-on, so a timer must never
# fire in a tight loop however confused the stored timestamps are.
_MINIMUM_DELAY_SECONDS = 60

# Long enough after startup that Orca has finished announcing the desktop and
# the user is not interrupted mid-sentence by a dialog.
STARTUP_DELAY_SECONDS = 20


def interval_seconds(value: str) -> int:
    """Returns the interval in seconds, or 0 when checking is off."""

    return _BY_VALUE.get(value, _BY_VALUE[INTERVAL_DEFAULT])


class Scheduler:
    """Calls `on_check` on the configured interval, and once after startup."""

    def __init__(
        self,
        interval: Callable[[], str],
        last_check: Callable[[], int],
        record_check: Callable[[int], None],
        on_check: Callable[[], None],
        check_at_startup: Callable[[], bool] | None = None,
    ) -> None:
        self._interval = interval
        self._last_check = last_check
        self._record_check = record_check
        self._on_check = on_check
        self._check_at_startup = check_at_startup or (lambda: True)
        self._lock = threading.Lock()
        self._source_id: int = 0
        # Bumped on every stop. A callback carrying a stale generation has
        # outlived the configuration that armed it and must not re-arm.
        self._generation: int = 0
        self._running = False

    # --- Lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Arms the first timer. Safe to call when already running."""

        with self._lock:
            if self._running:
                return
            self._running = True
            generation = self._generation

        if self._check_at_startup():
            self._arm(STARTUP_DELAY_SECONDS, generation)
        else:
            self._arm(self._delay_until_due(), generation)

    def stop(self) -> None:
        """Disarms the timer and invalidates any callback already queued."""

        with self._lock:
            self._running = False
            self._generation += 1
            if self._source_id:
                GLib.source_remove(self._source_id)
                self._source_id = 0

    @property
    def running(self) -> bool:
        """Returns whether a timer is armed."""

        return self._running

    def reschedule(self) -> None:
        """Re-arms after a settings change, without running a check."""

        self.stop()
        self.start()

    # --- Timing ----------------------------------------------------------

    def _delay_until_due(self) -> int:
        """Seconds until the next check is due, given when the last one was."""

        seconds = interval_seconds(self._interval())
        if seconds <= 0:
            return 0
        due = self._last_check() + seconds
        return max(_MINIMUM_DELAY_SECONDS, int(due - time.time()))

    def _arm(self, delay: int, generation: int) -> None:
        if delay <= 0:
            return
        with self._lock:
            if generation != self._generation or not self._running:
                return
            if self._source_id:
                GLib.source_remove(self._source_id)
            self._source_id = GLib.timeout_add_seconds(
                delay, self._fire, generation
            )

    def _fire(self, generation: int) -> bool:
        with self._lock:
            self._source_id = 0
            if generation != self._generation or not self._running:
                return False

        if interval_seconds(self._interval()) <= 0:
            # Switched off while we were waiting. Stay stopped rather than
            # re-arming at some default.
            return False

        self._record_check(int(time.time()))
        try:
            self._on_check()
        finally:
            self._arm(self._delay_until_due(), generation)
        return False

    def note_check_completed(self) -> None:
        """Records a check that happened outside the timer, e.g. by keystroke."""

        self._record_check(int(time.time()))
