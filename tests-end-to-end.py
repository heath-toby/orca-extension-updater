#!/usr/bin/env python3
"""Extension Updater -- end-to-end test, against the real GitHub.

Run after ./install.sh:

    ./tests-end-to-end.py

This is the only test that uses the network. It downloads a real release of a
real add-on and installs it into a throwaway extensions directory, so the
whole path runs for real: lookup, download, extraction, compile check,
staging, the swap, approval and the reload. Orca's loader is faked, because
the point is to exercise the installer without touching the live screen
reader.

It also answers the question the progress bar depends on -- whether the
fraction actually moves while bytes arrive -- by recording every value the
installer reports and checking it rises.

Costs two or three of GitHub's sixty unauthenticated requests an hour.
"""

import os
import shutil
import sys
import tempfile
import types

import orca.extension_loader  # noqa: F401  (import order: see install.sh)

sys.path.insert(0, os.path.expanduser("~/.local/share/orca/extensions"))

from gi.repository import GLib  # noqa: E402

from extension_updater import installer as I  # noqa: E402
from extension_updater.github import Client  # noqa: E402
from extension_updater.inventory import Managed  # noqa: E402

REPO = "heath-toby/polyglot-for-orca"
PACKAGE = "polyglot"
LIVE = os.path.expanduser(f"~/.local/share/orca/extensions/{PACKAGE}")

rows, fails = [], 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    rows.append((name, repr(want), repr(got), "ok" if ok else "FAIL"))


class FakeLoader:
    def __init__(self):
        self.approved = []
        self.reloads = []

    def approve_extension_file(self, filepath):
        self.approved.append(filepath)
        return "hash"

    def reload_user_extension(self, _directory, source_id):
        self.reloads.append(source_id)

    def get_loaded_user_extension(self, _class_name):
        return object()


def tree(root):
    found = set()
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            if name.endswith((".pyc", ".pyo")):
                continue
            found.add(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/"))
    return found


root = tempfile.mkdtemp(prefix="eu-e2e-")
extensions = os.path.join(root, "extensions")
os.makedirs(os.path.join(extensions, PACKAGE))
# Stand in for an older installed release, so an update is genuinely due.
with open(os.path.join(extensions, PACKAGE, "__init__.py"), "w", encoding="utf-8") as handle:
    handle.write('__version__ = "2.9.0"\n\n\nclass Polyglot:\n    VERSION = "2.9.0"\n')

loader = FakeLoader()
fake_module = types.ModuleType("orca.extension_loader")
fake_module.get_loader = lambda: loader
real_module = sys.modules.get("orca.extension_loader")
import orca as orca_package  # noqa: E402

real_attribute = getattr(orca_package, "extension_loader", None)
sys.modules["orca.extension_loader"] = fake_module
orca_package.extension_loader = fake_module

original_data_dir = I.data_dir
I.data_dir = lambda: os.path.join(root, "data")

loop = GLib.MainLoop()
fractions = []
captured = {}
ticks = {"n": 0}
GLib.timeout_add(10, lambda: (ticks.__setitem__("n", ticks["n"] + 1), True)[1])

item = Managed(
    source_id=PACKAGE,
    class_name="Polyglot",
    label="Polyglot",
    version="2.9.0",
    repo=REPO,
    filepath=os.path.join(extensions, PACKAGE),
    is_package=True,
)


def finished(result):
    captured["result"] = result
    loop.quit()


def start():
    def found(tag, error):
        if error is not None or not tag:
            captured["lookup_error"] = error or "no tag"
            loop.quit()
            return
        captured["tag"] = tag
        I.Installer(extensions).update(item, tag, fractions.append, finished)

    Client().latest_version(REPO, found)
    return False


GLib.idle_add(start)
GLib.timeout_add_seconds(120, lambda: (loop.quit(), False)[1])
loop.run()

try:
    check("the release tag was found", "lookup_error" in captured, False)
    result = captured.get("result")
    check("the update ran to completion", result is not None, True)

    if result is not None:
        check("the update succeeded", result.ok, True)
        check("the update reports no error", result.error, None)
        check("the update downloaded something", result.downloaded_bytes > 0, True)
        check("the new version is the release tag",
              result.to_version, captured.get("tag", "").lstrip("v"))
        check("the release ships its own install.sh", result.has_install_script, True)
        check("the update was approved once", len(loader.approved), 1)
        check("the update was reloaded once", loader.reloads, [PACKAGE])

    # The progress bar is only useful if the fraction actually moves.
    check("progress was reported", len(fractions) > 1, True)
    check("progress never goes backwards",
          all(b >= a - 1e-9 for a, b in zip(fractions, fractions[1:])), True)
    check("progress starts at or below the download phase",
          bool(fractions) and fractions[0] <= I.DOWNLOAD_END, True)
    check("progress ends at 1.0", fractions[-1] if fractions else None, 1.0)
    check("progress moved inside the download phase",
          len([f for f in fractions if 0 < f < I.DOWNLOAD_END]) >= 1, True)
    check("progress passed through the validate phase",
          any(I.DOWNLOAD_END < f <= I.VALIDATE_END for f in fractions), True)
    check("progress passed through the install phase",
          any(I.VALIDATE_END < f < 1.0 for f in fractions), True)

    # Orca's main loop kept running throughout, which is the whole point of
    # doing the download asynchronously.
    check("the main loop was never blocked", ticks["n"] > 20, True)

    # The installed tree must be what this add-on's own install.sh produces.
    installed = os.path.join(extensions, PACKAGE)
    check("the installed tree matches a hand-installed one",
          tree(installed), tree(LIVE))
    init = os.path.join(installed, "__init__.py")
    text = open(init, encoding="utf-8").read() if os.path.exists(init) else ""
    check("the installed version is the new one",
          f'__version__ = "{captured.get("tag", "").lstrip("v")}"' in text, True)
    check("no schema file was installed",
          [p for p in tree(installed) if p.endswith(".gschema.xml")], [])
    check("no staging directory was left behind",
          os.path.exists(os.path.join(root, "data", "staging", PACKAGE)), False)
    check("a rollback backup was kept",
          os.path.isdir(os.path.join(root, "data", "backups", PACKAGE)), True)
finally:
    I.data_dir = original_data_dir
    if real_module is not None:
        sys.modules["orca.extension_loader"] = real_module
    if real_attribute is not None:
        orca_package.extension_loader = real_attribute
    shutil.rmtree(root, ignore_errors=True)

width = max(len(row[0]) for row in rows)
print()
for name, want, got, status in rows:
    print(f"  {status:4s} {name:<{width}}  want={want}  got={got}")
print(f"\n{len(rows) - fails}/{len(rows)} rows green")
print(f"  (progress values: {[round(f, 3) for f in fractions]})")
sys.exit(1 if fails else 0)
