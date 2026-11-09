#!/usr/bin/env python3
"""Extension Updater -- scheduler tests.

Run after ./install.sh, against the installed copy:

    ./tests-schedule.py

GLib's timers are replaced with a recorder, so these rows assert on what the
scheduler *decided* -- the delay it asked for -- rather than waiting for real
time to pass. A test that waited an hour to check an hourly interval would
never be run.
"""

import os
import sys
import time

import orca.extension_loader  # noqa: F401  (import order: see install.sh)

sys.path.insert(0, os.path.expanduser("~/.local/share/orca/extensions"))

from extension_updater import scheduler as S  # noqa: E402

rows, fails = [], 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    rows.append((name, repr(want), repr(got), "ok" if ok else "FAIL"))


# --- Intervals ---------------------------------------------------------------

check("the interval list has no duplicate values",
      len({v for v, _l, _s in S.INTERVALS}), len(S.INTERVALS))
check("the default interval is in the list",
      S.INTERVAL_DEFAULT in {v for v, _l, _s in S.INTERVALS}, True)
check("every interval has a label", [v for v, l, _s in S.INTERVALS if not l], [])

for value, want in [
    ("hourly", 3600),
    ("6-hourly", 6 * 3600),
    ("12-hourly", 12 * 3600),
    ("daily", 24 * 3600),
    ("weekly", 7 * 24 * 3600),
    ("monthly", 30 * 24 * 3600),
    ("off", 0),
    # An unknown stored value falls back to the default rather than to "off":
    # silently stopping checks is the worse failure.
    ("nonsense", 24 * 3600),
    ("", 24 * 3600),
]:
    check(f"interval_seconds({value!r})", S.interval_seconds(value), want)


# --- A recorder in place of GLib's timers ------------------------------------

armed = []
real_add = S.GLib.timeout_add_seconds
real_remove = S.GLib.source_remove
S.GLib.timeout_add_seconds = lambda seconds, callback, *args: (
    armed.append((seconds, callback, args)),
    len(armed),
)[1]
S.GLib.source_remove = lambda _source_id: True


def build(interval="daily", last=0, startup=True, checks=None):
    """Returns a scheduler wired to plain values, and its recorded state."""

    state = {"last": last, "checks": 0}

    def record(when):
        state["last"] = when

    def on_check():
        state["checks"] += 1
        if checks is not None:
            checks.append(when_called())

    def when_called():
        return state["checks"]

    return (
        S.Scheduler(
            interval=lambda: interval,
            last_check=lambda: state["last"],
            record_check=record,
            on_check=on_check,
            check_at_startup=lambda: startup,
        ),
        state,
    )


try:
    # Startup check: armed at the startup delay, not at the interval.
    armed.clear()
    sched, _state = build(interval="daily", startup=True)
    sched.start()
    check("a startup check is armed", len(armed), 1)
    check("a startup check waits the startup delay",
          armed[0][0], S.STARTUP_DELAY_SECONDS)
    check("start() marks the scheduler running", sched.running, True)

    # Starting twice does not arm twice.
    sched.start()
    check("start() twice arms once", len(armed), 1)

    # With startup checks off, the first delay comes from the interval.
    armed.clear()
    sched, _state = build(interval="hourly", last=int(time.time()), startup=False)
    sched.start()
    check("without a startup check the interval is used",
          abs(armed[0][0] - 3600) <= 2, True)

    # A check long overdue is not run instantly in a loop; it waits the floor.
    armed.clear()
    sched, _state = build(interval="hourly", last=1, startup=False)
    sched.start()
    check("an overdue check waits the minimum delay",
          armed[0][0], S._MINIMUM_DELAY_SECONDS)

    # Off means nothing is armed at all.
    armed.clear()
    sched, _state = build(interval="off", startup=False)
    sched.start()
    check("off arms no timer", armed, [])

    # Firing runs the check, records the time, and re-arms.
    armed.clear()
    sched, state = build(interval="hourly", last=0, startup=True)
    sched.start()
    delay, callback, args = armed[0]
    before = int(time.time())
    callback(*args)
    check("firing runs the check", state["checks"], 1)
    check("firing records when it ran", state["last"] >= before, True)
    check("firing re-arms", len(armed), 2)
    check("the re-armed delay is the interval",
          abs(armed[1][0] - 3600) <= 2, True)

    # A callback left over from before a stop must not restart the schedule.
    armed.clear()
    sched, state = build(interval="hourly", startup=True)
    sched.start()
    _delay, callback, args = armed[0]
    sched.stop()
    check("stop() clears running", sched.running, False)
    callback(*args)
    check("a stale callback does not run a check", state["checks"], 0)
    check("a stale callback does not re-arm", len(armed), 1)

    # Switched off while waiting: stay stopped rather than fall back.
    armed.clear()
    interval_value = {"v": "hourly"}
    state = {"last": 0, "checks": 0}
    sched = S.Scheduler(
        interval=lambda: interval_value["v"],
        last_check=lambda: state["last"],
        record_check=lambda when: state.update(last=when),
        on_check=lambda: state.update(checks=state["checks"] + 1),
        check_at_startup=lambda: True,
    )
    sched.start()
    _delay, callback, args = armed[0]
    interval_value["v"] = "off"
    callback(*args)
    check("switching off while waiting runs no check", state["checks"], 0)
    check("switching off while waiting re-arms nothing", len(armed), 1)

    # reschedule() re-arms without running a check.
    armed.clear()
    sched, state = build(interval="daily", startup=True)
    sched.start()
    armed.clear()
    sched.reschedule()
    check("reschedule arms again", len(armed), 1)
    check("reschedule runs no check", state["checks"], 0)

    # A check made by keystroke still counts as a check.
    sched, state = build(interval="daily")
    before = int(time.time())
    sched.note_check_completed()
    check("an on-demand check is recorded", state["last"] >= before, True)
finally:
    S.GLib.timeout_add_seconds = real_add
    S.GLib.source_remove = real_remove

width = max(len(row[0]) for row in rows)
print()
for name, want, got, status in rows:
    print(f"  {status:4s} {name:<{width}}  want={want}  got={got}")
print(f"\n{len(rows) - fails}/{len(rows)} rows green")
sys.exit(1 if fails else 0)
