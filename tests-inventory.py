#!/usr/bin/env python3
"""Extension Updater -- inventory tests.

Run after ./install.sh, against the installed copy:

    ./tests-inventory.py

Covers the two decisions that decide whether an add-on gets updated at all:
which WEBSITE values name a repository, and which version is newer. The
second half runs against the extensions actually installed on this machine,
so it also reports anything the updater cannot check and why.
"""

import os
import sys

import orca.extension_loader  # noqa: F401  (import order: see install.sh)

sys.path.insert(0, os.path.expanduser("~/.local/share/orca/extensions"))

from extension_updater import inventory as inv  # noqa: E402

rows, fails = [], 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    rows.append((name, repr(want), repr(got), "ok" if ok else "FAIL"))


# --- Which websites name a repository ----------------------------------------

for website, want in [
    ("https://github.com/heath-toby/Clock", "heath-toby/Clock"),
    ("https://github.com/heath-toby/polyglot-for-orca", "heath-toby/polyglot-for-orca"),
    ("https://github.com/heath-toby/polyglot-for-orca/", "heath-toby/polyglot-for-orca"),
    ("https://github.com/heath-toby/polyglot-for-orca.git", "heath-toby/polyglot-for-orca"),
    ("http://github.com/heath-toby/Clock", "heath-toby/Clock"),
    ("https://www.github.com/heath-toby/Clock", "heath-toby/Clock"),
    ("  https://github.com/heath-toby/Clock  ", "heath-toby/Clock"),
    # A profile names no releases.
    ("https://github.com/heath-toby", None),
    # A file, a wiki page or an issue is not a project root.
    ("https://github.com/heath-toby/Clock/blob/main/README.md", None),
    ("https://github.com/heath-toby/Clock/wiki", None),
    # Another forge is not refused on principle, it is simply not implemented.
    ("https://gitlab.com/heath-toby/Clock", None),
    ("https://example.com/heath-toby/Clock", None),
    # Things that would resolve to a directory rather than a repository.
    ("https://github.com/heath-toby/.", None),
    ("https://github.com/heath-toby/..", None),
    ("", None),
    ("not a url", None),
]:
    check(f"parse_repo({website!r})", inv.parse_repo(website), want)


# --- Which versions compare --------------------------------------------------

for text, want in [
    ("2.10.0", (2, 10)),
    ("v2.10.0", (2, 10)),
    ("V2.10.0", None),
    ("2.1", (2, 1)),
    ("2.1.0", (2, 1)),
    ("2.0.0", (2,)),
    ("1", (1,)),
    ("0.0.1", (0, 0, 1)),
    ("2.10.0-rc1", None),
    ("latest", None),
    ("", None),
    ("v", None),
]:
    check(f"parse_version({text!r})", inv.parse_version(text), want)

for remote, installed, want in [
    # The ordinary case.
    ("v2.11.0", "2.10.0", True),
    ("v2.10.1", "2.10.0", True),
    ("v3.0.0", "2.10.0", True),
    # Equal is not newer, however it is spelled. 2.1 and 2.1.0 are one release.
    ("v2.10.0", "2.10.0", False),
    ("v2.1", "2.1.0", False),
    ("v2.1.0", "2.1", False),
    # Double-digit components compare as numbers, not as text: "2.9" must not
    # beat "2.10" the way it would in a string comparison.
    ("v2.9.0", "2.10.0", False),
    ("v2.10.0", "2.9.0", True),
    # A rolled-back tag must never drag a working install backwards.
    ("v1.0.0", "2.10.0", False),
    # An unknown version is not evidence of an update.
    ("latest", "2.10.0", False),
    ("v2.11.0", "", False),
    ("", "2.10.0", False),
]:
    check(f"is_newer({remote!r}, {installed!r})", inv.is_newer(remote, installed), want)


# --- Against what is actually installed --------------------------------------

managed, unmanaged = inv.discover()
by_id = {entry.source_id: entry for entry in managed}

check("the updater can see itself", "extension_updater" in by_id, True)
check(
    "every managed entry has an owner and a repo",
    sorted({entry.repo.count("/") for entry in managed}),
    [1],
)
check(
    "every managed entry has a comparable version",
    [e.source_id for e in managed if inv.parse_version(e.version) is None],
    [],
)
check(
    "every managed entry knows its class name",
    [e.source_id for e in managed if not e.class_name],
    [],
)
check(
    "nothing is both managed and unmanaged",
    sorted(set(by_id) & {e.source_id for e in unmanaged}),
    [],
)

expected = {
    "polyglot": "heath-toby/polyglot-for-orca",
    "capitalbeep": "heath-toby/orca-capitalbeep",
    "indent_beeper": "heath-toby/orca-indent-beeper",
    "caps_lock_fix": "heath-toby/orca-caps-lock-fix",
    "wayland_clipboard": "heath-toby/orca-wayland-clipboard",
    "audio_themes": "heath-toby/audio-themes-for-orca",
    "signal_filter": "heath-toby/orca-signal-filter",
    "clock": "heath-toby/Clock",
}
for source_id, repo in expected.items():
    entry = by_id.get(source_id)
    if entry is None:
        rows.append((f"{source_id} is managed", repo, "not installed", "skip"))
        continue
    check(f"{source_id} resolves to its repository", entry.repo, repo)

print()
print("  Installed extensions the updater cannot check:")
for entry in sorted(unmanaged, key=lambda e: e.source_id):
    print(f"    {entry.source_id:22s} {entry.reason}")
print()

width = max(len(row[0]) for row in rows)
for name, want, got, status in rows:
    print(f"  {status:4s} {name:<{width}}  want={want}  got={got}")
counted = [row for row in rows if row[3] != "skip"]
print(f"\n{len(counted) - fails}/{len(counted)} rows green")
sys.exit(1 if fails else 0)
