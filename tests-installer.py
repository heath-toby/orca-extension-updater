#!/usr/bin/env python3
"""Extension Updater -- installer tests.

Run after ./install.sh, against the installed copy:

    ./tests-installer.py

The load-bearing row is the copy-rule comparison. It installs nothing: it
runs the updater's staging routine over each add-on's real source tree and
diffs the result against what that add-on's own install.sh actually put in
~/.local/share/orca/extensions. If those trees ever differ, "never run the
downloaded install.sh" stops being safe and becomes a guess.

The rollback rows fake Orca's extension loader, because the failures they
describe -- a release that will not import, and one that raises while
building its keybindings -- must be provoked deliberately and must never be
provoked against the live screen reader.
"""

import filecmp
import io
import os
import shutil
import sys
import tarfile
import tempfile
import types

import orca.extension_loader  # noqa: F401  (import order: see install.sh)

sys.path.insert(0, os.path.expanduser("~/.local/share/orca/extensions"))

from extension_updater import installer as I  # noqa: E402
from extension_updater.inventory import Managed  # noqa: E402

DEV = os.path.expanduser("~/Development")
INSTALLED = os.path.expanduser("~/.local/share/orca/extensions")

rows, fails = [], 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    # Record what was compared, not a reference to it. These tests pass live
    # lists in, and a list appended to later would print its final state
    # beside a verdict reached much earlier.
    rows.append((name, repr(want), repr(got), "ok" if ok else "FAIL"))


# --- Path sanitising ---------------------------------------------------------
# These lists are read out of a downloaded __init__.py, so each of these is a
# path that must never be joined onto a destination.

for value, want in [
    ("sounds", "sounds"),
    ("themes", "themes"),
    ("ok_name", "ok_name"),
    ("../../etc", None),
    ("/etc/passwd", None),
    ("a/b", None),
    ("a\\b", None),
    (".hidden", None),
    ("..", None),
    (".", None),
    ("", None),
    ("   ", None),
    (5, None),
    (None, None),
]:
    check(f"_safe_name({value!r})", I._safe_name(value), want)


# --- The copy rule -----------------------------------------------------------

for relpath, extra, want in [
    ("clock.py", (), True),
    ("sounds/bell.wav", (), True),
    ("README", (), True),
    ("__pycache__/clock.cpython-314.pyc", (), False),
    ("clock.cpython-314.pyc", (), False),
    ("thing.pyo", (), False),
    ("org.gnome.Orca.Clock.gschema.xml", (), False),
    (".venv/bin/python", (), False),
    (".hidden", (), False),
    ("sub/.hidden", (), False),
    ("themes/default/button.wav", ("themes",), False),
    ("themes", ("themes",), False),
    ("sounds/bell.wav", ("themes",), True),
]:
    check(f"should_copy({relpath!r}, {extra!r})", I.should_copy(relpath, extra), want)


# --- Reading the dunders without importing -----------------------------------


def package_with(body):
    directory = tempfile.mkdtemp(prefix="eu-pkg-")
    with open(os.path.join(directory, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write(body)
    return directory


cases = [
    ('__update_exclude__ = ("themes",)\n', ("themes",), ()),
    ('__update_include__ = ("sounds",)\n', (), ("sounds",)),
    ('__update_exclude__ = ["a", "b"]\n', ("a", "b"), ()),
    # A malicious or confused entry is dropped, not normalised.
    ('__update_include__ = ("../../etc", "sounds")\n', (), ("sounds",)),
    ('__update_exclude__ = ("/etc",)\n', (), ()),
    # Not a literal: not evaluated, so not honoured.
    ('__update_exclude__ = tuple(open("/etc/passwd"))\n', (), ()),
    ("", (), ()),
    ("this is not python(\n", (), ()),
]
for body, want_exclude, want_include in cases:
    directory = package_with(body)
    label = body.strip().replace("\n", " ")[:46] or "empty"
    check(f"exclude from: {label}", I.read_update_exclude(directory), want_exclude)
    check(f"include from: {label}", I.read_update_include(directory), want_include)
    shutil.rmtree(directory, ignore_errors=True)

# A package that does not exist must not raise.
check("exclude from a missing package", I.read_update_exclude("/nonexistent"), ())


# --- Staging reproduces install.sh -------------------------------------------
# The whole justification for not running a downloaded install.sh.

PAIRS = [
    ("polyglot", f"{DEV}/polyglot-for-orca", "polyglot"),
    ("capitalbeep", f"{DEV}/orca-capitalbeep", "capitalbeep"),
    ("indent_beeper", f"{DEV}/orca-indent-beeper", "indent_beeper"),
    ("caps_lock_fix", f"{DEV}/orca-caps-lock-fix", "caps_lock_fix"),
    ("wayland_clipboard", f"{DEV}/orca-wayland-clipboard", "wayland_clipboard"),
    ("audio_themes", f"{DEV}/orca-audio-themes", "audio_themes"),
    ("signal_filter", f"{DEV}/orca-signal-filter", "signal_filter"),
    ("clock", f"{DEV}/Clock", "clock"),
    ("lbrf_orca", f"{DEV}/lbrf-orca", "lbrf_orca"),
]


def tree(root):
    found = set()
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            if name.endswith((".pyc", ".pyo")):
                continue
            found.add(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/"))
    return found


installer = I.Installer(INSTALLED)
for name, repo, package in PAIRS:
    source = os.path.join(repo, package)
    target = os.path.join(INSTALLED, name)
    if not os.path.isdir(source) or not os.path.isdir(target):
        rows.append((f"staging matches install.sh: {name}", "-", "not present", "skip"))
        continue
    staging = tempfile.mkdtemp(prefix=f"eu-stage-{name}-")
    shutil.rmtree(staging)
    installer._stage(source, staging, repo)
    want, got = tree(target), tree(staging)
    # Which files get installed is the install.sh-equivalence check.
    check(f"staging matches install.sh: {name}", got, want)
    # Whether their bytes survive the copy is a separate question, and it is
    # asked against the *source*, not against the installed copy. Comparing
    # with the installed copy would only hold while the two are in step, so
    # bumping a version without reinstalling would fail a row that is
    # supposed to be about copying faithfully.
    differing = sorted(
        path
        for path in got
        if os.path.exists(os.path.join(source, path))
        and not filecmp.cmp(
            os.path.join(source, path), os.path.join(staging, path), shallow=False
        )
    )
    check(f"staged bytes match the source: {name}", differing, [])
    shutil.rmtree(staging, ignore_errors=True)


# --- Extraction refuses to write outside the destination ---------------------


def tarball_with(members):
    handle = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    handle.close()
    with tarfile.open(handle.name, "w:gz") as archive:
        for member_name, payload in members:
            info = tarfile.TarInfo(member_name)
            data = payload.encode("utf-8")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return handle.name


hostile = tarball_with(
    [
        ("root/pkg/__init__.py", "x = 1\n"),
        ("../escaped.py", "x = 1\n"),
        ("/tmp/eu-absolute-escape.py", "x = 1\n"),
        ("root/../../also-escaped.py", "x = 1\n"),
    ]
)
destination = tempfile.mkdtemp(prefix="eu-extract-")
refused = False
try:
    installer._extract(hostile, destination)
except I._Failed:
    refused = True
escaped = [
    path
    for path in (
        os.path.join(os.path.dirname(destination), "escaped.py"),
        "/tmp/eu-absolute-escape.py",
        os.path.join(os.path.dirname(destination), "also-escaped.py"),
    )
    if os.path.exists(path)
]
check("a traversing archive is refused or contained", refused or not escaped, True)
check("nothing was written outside the destination", escaped, [])
os.unlink(hostile)
shutil.rmtree(destination, ignore_errors=True)

# A well-formed archive extracts.
benign = tarball_with([("root/pkg/__init__.py", "VERSION = '1.0.0'\n")])
destination = tempfile.mkdtemp(prefix="eu-extract-ok-")
installer._extract(benign, destination)
check(
    "a well-formed archive extracts",
    os.path.isfile(os.path.join(destination, "root", "pkg", "__init__.py")),
    True,
)
os.unlink(benign)
shutil.rmtree(destination, ignore_errors=True)


# --- Install, fail, and come back --------------------------------------------
# Orca's loader is faked here. The three failures below are the three shapes
# a bad release takes, and the third is the dangerous one: Orca does not
# guard _get_commands(), so a release that raises there would not fail now,
# it would fail at the next start with no add-on running to undo it.


class FakeLoader:
    """Mimics the parts of ExtensionLoader the installer uses.

    `raise_times` and `fail_loads` count down, so a loader can fail the
    update's reload and then succeed on the rollback's reload -- which is
    the difference between a recoverable failure and an unrecoverable one,
    and the two have to be told apart.
    """

    def __init__(self, raise_times=0, fail_loads=0):
        self.raise_times = raise_times
        self.fail_loads = fail_loads
        self.approved = []
        self.reloads = []

    def approve_extension_file(self, filepath):
        self.approved.append(filepath)
        return "hash"

    def reload_user_extension(self, _directory, source_id):
        self.reloads.append(source_id)
        if self.raise_times > 0:
            self.raise_times -= 1
            raise RuntimeError("_get_commands() blew up")

    def get_loaded_user_extension(self, _class_name):
        if self.fail_loads > 0:
            self.fail_loads -= 1
            return None
        return object()


def install_fixture():
    """Builds an extensions dir with one installed package, and a release."""

    root = tempfile.mkdtemp(prefix="eu-fixture-")
    extensions = os.path.join(root, "extensions")
    os.makedirs(os.path.join(extensions, "demo"))
    with open(os.path.join(extensions, "demo", "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write('VERSION = "1.0.0"\n')
    return root, extensions


def run_install(package_body, loader, extra_files=()):
    """Runs the whole post-download path and returns (result, installed_text)."""

    root, extensions = install_fixture()
    release = os.path.join(root, "release", "heath-toby-demo-abc123")
    os.makedirs(os.path.join(release, "demo"))
    with open(os.path.join(release, "demo", "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write(package_body)
    for name, body in extra_files:
        path = os.path.join(release, "demo", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
    tarball = os.path.join(root, "release.tar.gz")
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(release, arcname="heath-toby-demo-abc123")

    fake = types.ModuleType("orca.extension_loader")
    fake.get_loader = lambda: loader
    real = sys.modules.get("orca.extension_loader")
    sys.modules["orca.extension_loader"] = fake
    # `from orca import extension_loader` resolves the attribute on the
    # already-imported orca package, not sys.modules, so both have to be
    # swapped or the installer keeps talking to the real loader -- and the
    # test silently measures nothing.
    import orca as _orca_package

    real_attribute = getattr(_orca_package, "extension_loader", None)
    _orca_package.extension_loader = fake

    data = os.path.join(root, "data")
    original_data_dir = I.data_dir
    I.data_dir = lambda: data

    captured = {}
    try:
        item = Managed(
            source_id="demo",
            class_name="Demo",
            label="Demo",
            version="1.0.0",
            repo="heath-toby/demo",
            filepath=os.path.join(extensions, "demo"),
            is_package=True,
        )
        result = I.Result(label="Demo", source_id="demo", from_version="1.0.0", to_version="2.0.0")
        workdir = os.path.join(root, "work")
        os.makedirs(workdir)
        shutil.copy2(tarball, os.path.join(workdir, "release.tar.gz"))
        inst = I.Installer(extensions)
        inst._install(
            item,
            os.path.join(workdir, "release.tar.gz"),
            workdir,
            result,
            lambda _fraction: None,
            lambda outcome: captured.setdefault("result", outcome),
        )
    finally:
        I.data_dir = original_data_dir
        if real is not None:
            sys.modules["orca.extension_loader"] = real
        else:
            sys.modules.pop("orca.extension_loader", None)
        if real_attribute is not None:
            _orca_package.extension_loader = real_attribute

    installed = os.path.join(extensions, "demo", "__init__.py")
    text = open(installed, encoding="utf-8").read() if os.path.exists(installed) else None
    outcome = captured.get("result")
    shutil.rmtree(root, ignore_errors=True)
    return outcome, text


# (a) It works.
loader = FakeLoader()
outcome, text = run_install('VERSION = "2.0.0"\n', loader)
check("a good release installs", outcome.ok, True)
check("a good release leaves no error", outcome.error, None)
check("a good release is on disk", text, 'VERSION = "2.0.0"\n')
check("a good release is approved", len(loader.approved), 1)
check("a good release is reloaded", loader.reloads, ["demo"])

# (a2) Extra files in the release are installed too.
loader = FakeLoader()
outcome, _text = run_install(
    'VERSION = "2.0.0"\n', loader, extra_files=(("helper.py", "x = 1\n"),)
)
check("a release with two modules installs", outcome.ok, True)

# (b) A syntax error never reaches the extensions directory.
loader = FakeLoader()
outcome, text = run_install("def broken(:\n", loader)
check("a syntax error is caught", outcome.ok, False)
check("a syntax error is reported as one", "syntax error" in (outcome.error or ""), True)
check("a syntax error leaves the old version installed", text, 'VERSION = "1.0.0"\n')
check("a syntax error means nothing was approved", loader.approved, [])
check("a syntax error means nothing was reloaded", loader.reloads, [])

# (c) A release that imports but does not register is rolled back. Orca
# catches a failed import itself and simply returns None, saying nothing out
# loud, so the absence has to be checked for rather than waited for.
loader = FakeLoader(fail_loads=1)
outcome, text = run_install('VERSION = "2.0.0"\n', loader)
check("a release that will not load fails", outcome.ok, False)
check("a release that will not load is rolled back", text, 'VERSION = "1.0.0"\n')
check("the rollback is re-approved", len(loader.approved), 2)
check("the rollback is reloaded", loader.reloads, ["demo", "demo"])
check("a rollback that worked is not unrecoverable", outcome.unrecoverable, False)

# (d) The dangerous one: a release whose _get_commands() raises. Orca does
# not guard that call, so without the try/except around our own reload this
# would not fail here at all -- it would fail at the next Orca start.
loader = FakeLoader(raise_times=1)
outcome, text = run_install('VERSION = "2.0.0"\n', loader)
check("a raising _get_commands is caught", outcome.ok, False)
check("a raising _get_commands is rolled back", text, 'VERSION = "1.0.0"\n')
check(
    "a raising _get_commands says the version failed to load",
    "failed to load" in (outcome.error or ""),
    True,
)
check("a recovered rollback is not unrecoverable", outcome.unrecoverable, False)

# (d2) And when the rollback cannot be brought back up either, that is the
# one state the add-on cannot repair from inside Orca, so it must say so.
loader = FakeLoader(raise_times=99)
outcome, text = run_install('VERSION = "2.0.0"\n', loader)
check("a failed rollback is flagged unrecoverable", outcome.unrecoverable, True)
check(
    "a failed rollback says to restart Orca",
    "Restart Orca" in (outcome.error or ""),
    True,
)

# (e) Updating the updater itself: installed and approved, but not reloaded,
# because reloading would re-import this very module underneath the code doing
# the reloading. It must be reported as needing a restart, not as live.
loader = FakeLoader()
root, extensions = install_fixture()
try:
    release = os.path.join(root, "rel", "heath-toby-demo-abc123", "demo")
    os.makedirs(release)
    with open(os.path.join(release, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write('VERSION = "2.0.0"\n')
    tarball = os.path.join(root, "self.tar.gz")
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(os.path.dirname(release), arcname="heath-toby-demo-abc123")

    fake = types.ModuleType("orca.extension_loader")
    fake.get_loader = lambda: loader
    real = sys.modules.get("orca.extension_loader")
    import orca as orca_pkg

    real_attr = getattr(orca_pkg, "extension_loader", None)
    sys.modules["orca.extension_loader"] = fake
    orca_pkg.extension_loader = fake
    original = I.data_dir
    I.data_dir = lambda: os.path.join(root, "data")
    captured = {}
    try:
        item = Managed("demo", "Demo", "Demo", "1.0.0", "heath-toby/demo",
                       os.path.join(extensions, "demo"), True)
        result = I.Result("Demo", "demo", "1.0.0", "2.0.0")
        workdir = os.path.join(root, "work")
        os.makedirs(workdir)
        shutil.copy2(tarball, os.path.join(workdir, "release.tar.gz"))
        # self_source_id matches, so this is the updater updating itself.
        I.Installer(extensions, self_source_id="demo")._install(
            item,
            os.path.join(workdir, "release.tar.gz"),
            workdir,
            result,
            lambda _f: None,
            lambda outcome: captured.setdefault("result", outcome),
        )
    finally:
        I.data_dir = original
        if real is not None:
            sys.modules["orca.extension_loader"] = real
        if real_attr is not None:
            orca_pkg.extension_loader = real_attr

    outcome = captured["result"]
    check("updating itself succeeds", outcome.ok, True)
    check("updating itself is approved", len(loader.approved), 1)
    check("updating itself does NOT reload", loader.reloads, [])
    check("updating itself asks for a restart", outcome.restart_required, True)
    installed = open(os.path.join(extensions, "demo", "__init__.py"), encoding="utf-8").read()
    check("updating itself writes the new version", installed, 'VERSION = "2.0.0"\n')
finally:
    shutil.rmtree(root, ignore_errors=True)


# (f) A release that does not contain the package at all.
loader = FakeLoader()
root, extensions = install_fixture()
try:
    empty = os.path.join(root, "empty", "heath-toby-demo-abc123", "other")
    os.makedirs(empty)
    with open(os.path.join(empty, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write("x = 1\n")
    tarball = os.path.join(root, "empty.tar.gz")
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(os.path.dirname(empty), arcname="heath-toby-demo-abc123")
    extracted = os.path.join(root, "x")
    inst = I.Installer(extensions)
    inst._extract(tarball, extracted)
    missing = False
    try:
        inst._find_package(
            extracted,
            Managed("demo", "Demo", "Demo", "1.0.0", "heath-toby/demo", "", True),
        )
    except I._Failed:
        missing = True
    check("a release without the package is refused", missing, True)
finally:
    shutil.rmtree(root, ignore_errors=True)


width = max(len(row[0]) for row in rows)
print()
for name, want, got, status in rows:
    print(f"  {status:4s} {name:<{width}}  want={want}  got={got}")
counted = [row for row in rows if row[3] != "skip"]
print(f"\n{len(counted) - fails}/{len(counted)} rows green")
sys.exit(1 if fails else 0)
