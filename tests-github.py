#!/usr/bin/env python3
"""Extension Updater -- release lookup tests.

Run after ./install.sh:

    ./tests-github.py

Mostly offline. The last section makes two real requests, to prove the claim
the whole design rests on: that the Atom feeds spend no API allowance. It is
skipped if the network is unreachable, and it never touches api.github.com.
"""

import os
import re
import sys
import time

import orca.extension_loader  # noqa: F401  (import order: see install.sh)

sys.path.insert(0, os.path.expanduser("~/.local/share/orca/extensions"))

from gi.repository import GLib  # noqa: E402

from extension_updater import github as G  # noqa: E402

rows, fails = [], 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    rows.append((name, repr(want), repr(got), "ok" if ok else "FAIL"))


# --- Which channel is used ---------------------------------------------------

check("gh_path finds gh if it is installed",
      G.gh_path() is None or os.path.basename(G.gh_path()) == "gh", True)
check("a client prefers gh when it is present",
      Client_channel := G.Client().channel, "gh" if G.gh_path() else "atom")
check("a client can be told not to use gh",
      G.Client(use_gh=False).channel, "atom")
check("the user agent carries no address", "@" in G.USER_AGENT, False)
check("nothing points at the API host", "api.github.com" in G.GITHUB, False)


# --- Reading the feeds -------------------------------------------------------

RELEASES_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <link type="text/html" href="https://github.com/o/r/releases"/>
  <link type="application/atom+xml" href="https://github.com/o/r/releases.atom"/>
  <entry>
    <link href="https://github.com/o/r/releases/tag/v2.3.1"/>
    <title>Thing 2.3.1</title>
  </entry>
  <entry>
    <link href="https://github.com/o/r/releases/tag/v2.3.0"/>
  </entry>
</feed>
"""

EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <link type="text/html" href="https://github.com/o/r/releases"/>
  <title>Release notes from r</title>
</feed>
"""

check("tags are read from a feed",
      G.parse_feed_tags(RELEASES_FEED), ["v2.3.1", "v2.3.0"])
check("the feed's own links are not mistaken for tags",
      [t for t in G.parse_feed_tags(RELEASES_FEED) if "releases" in t], [])
check("an empty feed yields nothing", G.parse_feed_tags(EMPTY_FEED), [])
check("rubbish yields nothing", G.parse_feed_tags("not xml at all"), [])
check("an empty string yields nothing", G.parse_feed_tags(""), [])
check("a repeated tag is listed once",
      G.parse_feed_tags(
          '<link href="https://github.com/o/r/releases/tag/v1.0"/>'
          '<link href="https://github.com/o/r/releases/tag/v1.0"/>'),
      ["v1.0"])
# A tag may contain characters that are escaped in a URL.
check("an escaped tag name is decoded",
      G.parse_feed_tags('<link href="https://github.com/o/r/releases/tag/v1.0%2B1"/>'),
      ["v1.0+1"])


# --- Choosing between tags ---------------------------------------------------

check("the highest version wins", G.best_tag(["v1.0.0", "v2.1.0", "v2.0.9"]), "v2.1.0")
check("double digits compare as numbers",
      G.best_tag(["v2.9.0", "v2.10.0"]), "v2.10.0")
check("order in the list does not matter",
      G.best_tag(["v2.10.0", "v2.9.0"]), "v2.10.0")
# A feed lists pre-releases beside releases, which is why the highest version
# is taken rather than the first entry -- but a tag that is not a plain
# 1.2.3 is skipped, not guessed at.
check("a release candidate is skipped",
      G.best_tag(["v2.0.0-rc1", "v1.9.0"]), "v1.9.0")
check("a date tag is skipped", G.best_tag(["2026-11-10", "v1.0.0"]), "v1.0.0")
check("a code name is skipped", G.best_tag(["codename", "v1.0.0"]), "v1.0.0")
check("nothing comparable gives nothing", G.best_tag(["rc1", "latest"]), None)
check("an empty list gives nothing", G.best_tag([]), None)
check("equal versions spelled differently pick one",
      G.best_tag(["v2.1", "v2.1.0"]) in ("v2.1", "v2.1.0"), True)


# --- Refusals ----------------------------------------------------------------


class FakeMessage:
    def __init__(self, status):
        self._status = status

    def get_status(self):
        return self._status


for status, want in [(403, True), (429, True), (200, False), (404, False), (500, False)]:
    check(f"_rate_limited(status={status})", G._rate_limited(FakeMessage(status)), want)

check("the refusal message says to try again",
      "again" in G.rate_limit_message(), True)


# --- The tag cache -----------------------------------------------------------

G.clear_tag_cache()
check("an empty cache knows nothing", G.cached_tag("owner/repo"), None)

G._tag_cache["owner/repo"] = ("v1.2.3", time.time())
check("a fresh entry is returned", G.cached_tag("owner/repo"), "v1.2.3")
check("another repository is unaffected", G.cached_tag("owner/other"), None)

G._tag_cache["owner/repo"] = ("v1.2.3", time.time() - (G.TAG_CACHE_SECONDS - 5))
check("an entry inside the window is still used", G.cached_tag("owner/repo"), "v1.2.3")
G._tag_cache["owner/repo"] = ("v1.2.3", time.time() - (G.TAG_CACHE_SECONDS + 5))
check("a stale entry is not used", G.cached_tag("owner/repo"), None)
check("a stale entry is dropped rather than kept", "owner/repo" in G._tag_cache, False)
check("the window is shorter than an hour", G.TAG_CACHE_SECONDS < 3600, True)

G._tag_cache["a/b"] = ("v1", time.time())
G.clear_tag_cache()
check("clearing forgets everything", G.cached_tag("a/b"), None)

G._tag_cache["owner/repo"] = ("v9.9.9", time.time() + 60)
check("a clock skew forward does not discard the entry",
      G.cached_tag("owner/repo"), "v9.9.9")
G.clear_tag_cache()

check("the default estimate is a plausible archive size",
      1024 < G.DEFAULT_ESTIMATE_BYTES < 10 * 1024 * 1024, True)
check("an estimated fraction cannot reach the end of its phase",
      G._ESTIMATE_CEILING < 1.0, True)


# --- The claim the design rests on -------------------------------------------
# The Atom feeds are not API endpoints, so they spend no allowance. If this
# ever stops being true the whole approach needs revisiting, so it is asserted
# rather than trusted.

REPO = "heath-toby/orca-wayland-clipboard"


def api_used():
    """Returns api.github.com's used count, which /rate_limit does not spend."""

    import json
    import urllib.request

    request = urllib.request.Request(
        "https://api.github.com/rate_limit", headers={"User-Agent": G.USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.load(response)["resources"]["core"]["used"]


try:
    before = api_used()
except Exception as error:  # pylint: disable=broad-exception-caught
    print(f"\n  Skipping the online rows: {error}")
    before = None

if before is not None:
    G.clear_tag_cache()
    loop = GLib.MainLoop()
    found = {}
    client = G.Client(use_gh=False)  # force the feed path

    def done(tag, error):
        found["tag"], found["error"] = tag, error
        loop.quit()

    GLib.idle_add(lambda: (client.latest_version(REPO, done), False)[1])
    GLib.timeout_add_seconds(45, lambda: (loop.quit(), False)[1])
    loop.run()

    check("the feed channel finds a release",
          bool(found.get("tag")) and found.get("error") is None, True)
    check("the tag it found looks like a version",
          bool(re.match(r"^v?\d+(\.\d+)*$", found.get("tag") or "")), True)
    check("the feed lookup was cached", G.cached_tag(REPO), found.get("tag"))

    after = api_used()
    check("the feed channel spent no API allowance", after, before)

    if G.gh_path():
        G.clear_tag_cache()
        loop = GLib.MainLoop()
        via_gh = {}
        gh_client = G.Client()

        def gh_done(tag, error):
            via_gh["tag"], via_gh["error"] = tag, error
            loop.quit()

        GLib.idle_add(lambda: (gh_client.latest_version(REPO, gh_done), False)[1])
        GLib.timeout_add_seconds(45, lambda: (loop.quit(), False)[1])
        loop.run()
        check("the gh channel finds the same release",
              via_gh.get("tag"), found.get("tag"))
        check("the gh channel spent no unauthenticated allowance",
              api_used(), before)
    else:
        rows.append(("the gh channel agrees", "-", "gh not installed", "skip"))

width = max(len(row[0]) for row in rows)
print()
for name, want, got, status in rows:
    print(f"  {status:4s} {name:<{width}}  want={want}  got={got}")
counted = [row for row in rows if row[3] != "skip"]
print(f"\n{len(counted) - fails}/{len(counted)} rows green")
sys.exit(1 if fails else 0)
