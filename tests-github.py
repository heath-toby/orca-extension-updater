#!/usr/bin/env python3
"""Extension Updater -- GitHub budget tests.

Run after ./install.sh:

    ./tests-github.py

No network. These rows cover the allowance bookkeeping, which is the thing
that went wrong in the field: a run of eight updates installed seven and
failed the last with a bare "403", because the hourly limit had been spent
and nothing said so.
"""

import os
import sys
import time

import orca.extension_loader  # noqa: F401  (import order: see install.sh)

sys.path.insert(0, os.path.expanduser("~/.local/share/orca/extensions"))

from extension_updater import github as G  # noqa: E402

rows, fails = [], 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    rows.append((name, repr(want), repr(got), "ok" if ok else "FAIL"))


saved = G.budget()
try:
    # Unknown allowance: assume nothing, block nothing.
    G._budget["remaining"], G._budget["reset"] = None, None
    check("an unknown allowance is not treated as spent", G._exhausted(), False)
    check("an unknown allowance still has a message",
          "used up" in G.rate_limit_message(), True)

    # Plenty left.
    G._budget["remaining"], G._budget["reset"] = 42, int(time.time()) + 600
    check("a healthy allowance is not spent", G._exhausted(), False)
    check("budget() reports what was recorded", G.budget()[0], 42)

    # Spent, and not yet reset.
    G._budget["remaining"] = 0
    check("a spent allowance is spent", G._exhausted(), True)
    message = G.rate_limit_message()
    check("the message names the limit", "60 requests" in message, True)
    check("the message says when it resets", "resets in" in message, True)
    check("the message gives a figure in minutes", "minute" in message, True)

    # One minute is singular. Small thing, but the message is spoken aloud.
    G._budget["reset"] = int(time.time()) + 10
    check("one minute is singular",
          G.rate_limit_message().endswith("in 1 minute"), True)
    G._budget["reset"] = int(time.time()) + 125
    check("more than one minute is plural",
          G.rate_limit_message().endswith("in 3 minutes"), True)

    # Spent but the window has already rolled over: not a blocker any more.
    G._budget["reset"] = int(time.time()) - 5
    check("a reset that has passed unblocks", G._exhausted(), False)
    check("a passed reset drops the countdown",
          "resets in" in G.rate_limit_message(), False)

    # Ordinary refusals are not the rate limit and must not be reported as it.
    class FakeHeaders:
        def __init__(self, remaining):
            self._remaining = remaining

        def get_one(self, name):
            return self._remaining if name == "x-ratelimit-remaining" else None

    class FakeMessage:
        def __init__(self, status, remaining):
            self._status, self._headers = status, FakeHeaders(remaining)

        def get_status(self):
            return self._status

        def get_response_headers(self):
            return self._headers

    for status, remaining, want in [
        (403, "0", True),
        (429, "0", True),
        (403, "17", False),   # refused for some other reason
        (403, None, False),   # no header at all
        (404, "0", False),    # a missing repo is not a rate limit
        (200, "0", False),    # the last request of the hour still succeeded
        (500, "0", False),
    ]:
        check(f"_rate_limited(status={status}, remaining={remaining!r})",
              G._rate_limited(FakeMessage(status, remaining)), want)

    # Headers are read off a response, including a successful one.
    G._budget["remaining"], G._budget["reset"] = None, None

    class FakeFullMessage:
        def __init__(self, headers):
            self._headers = headers

        def get_response_headers(self):
            class H:
                def get_one(_self, name):
                    return headers.get(name)
            headers = self._headers
            return H()

    G._note_budget(FakeFullMessage({"x-ratelimit-remaining": "58",
                                    "x-ratelimit-reset": "1794269252"}))
    check("remaining is recorded from a response", G.budget()[0], 58)
    check("the reset time is recorded from a response", G.budget()[1], 1794269252)

    # Junk headers must not crash, and must not overwrite good values.
    G._note_budget(FakeFullMessage({"x-ratelimit-remaining": "not a number"}))
    check("a junk header is ignored", G.budget()[0], 58)
    G._note_budget(FakeFullMessage({}))
    check("a missing header leaves the record alone", G.budget()[0], 58)
finally:
    G._budget["remaining"], G._budget["reset"] = saved

check("the default estimate is a plausible tarball size",
      1024 < G.DEFAULT_ESTIMATE_BYTES < 10 * 1024 * 1024, True)
check("an estimated fraction cannot reach the end of its phase",
      G._ESTIMATE_CEILING < 1.0, True)
check("the user agent carries no address", "@" in G.USER_AGENT, False)

width = max(len(row[0]) for row in rows)
print()
for name, want, got, status in rows:
    print(f"  {status:4s} {name:<{width}}  want={want}  got={got}")
print(f"\n{len(rows) - fails}/{len(rows)} rows green")
sys.exit(1 if fails else 0)
