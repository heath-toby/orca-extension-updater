"""Fetching an update and putting it in place, reversibly.

The order of operations here is the safety design, not an implementation
detail. An Orca extension is approved by a hash over every file in its
package, so installing an update means granting approval to code that was
just downloaded. Three things bound that:

* the repository comes from the *installed* extension's own metadata, never
  from anything in the download;
* nothing is swapped in until every Python file in the candidate compiles;
* the previous version is kept, and restored if any later step fails.

The last step is the load-bearing one. Orca catches a failed import, a
raising constructor and a raising ``on_ready`` -- but **not** a raising
``_get_commands()``, which propagates out of ``load_user_settings`` during
startup. A bad release that raises while building its keybindings would
therefore not fail now; it would fail at the next start, as a screen reader
that will not come up, with the add-on no longer running to undo it. So we
reload the extension ourselves, inside our own try/except, while Orca is
still up and a rollback is still possible.
"""

from __future__ import annotations

import ast
import glob
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field

from gi.repository import GLib

from .github import Client
from .inventory import Managed

# The fractions the plan fixes: download is the phase with real sub-progress
# and the one that can stall, so it owns half the bar.
DOWNLOAD_END = 0.50
VALIDATE_END = 0.75

# Never copied into an installed package. The first three are build and
# runtime litter; the fourth is a schema source that the add-ons' own
# install.sh scripts do not install either, and copying it would leave the
# installed tree differing from a hand-installed one for no benefit.
EXCLUDED_NAMES = ("__pycache__", ".venv")
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".gschema.xml")


@dataclass
class Result:
    """What happened to one extension."""

    label: str
    source_id: str
    from_version: str
    to_version: str
    ok: bool = False
    error: str | None = None
    downloaded_bytes: int = 0
    has_install_script: bool = False
    # Set when the new version is installed and approved but has not been
    # loaded, because the extension doing the updating cannot reload itself.
    restart_required: bool = False
    # Set when a rollback itself failed to come back up. The add-on cannot
    # repair this from inside Orca, so it must stop and say so.
    unrecoverable: bool = False


@dataclass
class _Paths:
    """Where the three working directories live for one update."""

    installed: str
    staging: str
    backup: str
    workdir: str = field(default="")


def data_dir() -> str:
    """Returns this add-on's own data directory, outside any package.

    Orca hashes every file in an extension's package, so anything this
    add-on writes -- logs, backups, staging trees -- has to live outside it
    or the first write would un-approve the extension.
    """

    return os.path.join(GLib.get_user_data_dir(), "orca", "extension-updater")


def _safe_name(value: object) -> str | None:
    """Accepts a single plain path segment and nothing else.

    These lists are read out of a downloaded ``__init__.py``, so they are
    untrusted input that is about to be joined onto a path. A name
    containing a separator, a parent reference, a drive or a leading dot is
    refused outright rather than normalised: there is no legitimate reason
    for an add-on to name anything but a directory or file beside its own
    source, and a rejected name costs one unfiltered file while an accepted
    "../.." costs the user's home directory.
    """

    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name in (".", ".."):
        return None
    if name.startswith(".") or os.path.isabs(name):
        return None
    if "/" in name or "\\" in name or os.sep in name:
        return None
    if (os.altsep or "") and os.altsep in name:
        return None
    return name


def _read_dunder_names(package_dir: str, dunder: str) -> tuple[str, ...]:
    """Reads a tuple-of-strings dunder from a package, without importing it.

    Parsed with ``ast`` for the same reason Orca parses its metadata that
    way: this is untrusted downloaded code and must not be executed to be
    inspected.
    """

    source = os.path.join(package_dir, "__init__.py")
    try:
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError, ValueError):
        return ()

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == dunder:
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError, MemoryError):
                    return ()
                if not isinstance(value, (list, tuple)):
                    return ()
                names = [_safe_name(entry) for entry in value]
                return tuple(name for name in names if name is not None)
    return ()


def read_update_exclude(package_dir: str) -> tuple[str, ...]:
    """Paths inside the package that must not be installed.

    Audio Themes is the add-on that needs this: its themes live outside the
    installed package deliberately, because editing one inside the package
    would silently un-approve the extension.
    """

    return _read_dunder_names(package_dir, "__update_exclude__")


def read_update_include(package_dir: str) -> tuple[str, ...]:
    """Paths beside the package, at the repository root, that belong in it.

    Clock is the add-on that needs this: its chime sounds live at the
    repository root and its install.sh copies them into the installed
    package, where the approval hash then covers them. Copying only the
    package directory would install Clock without any chimes.
    """

    return _read_dunder_names(package_dir, "__update_include__")


def should_copy(relpath: str, extra: tuple[str, ...]) -> bool:
    """Decides whether one path inside a candidate package is installed.

    The rule reproduces what the add-ons' own install.sh scripts produce,
    which a test asserts by diffing the two trees. It cannot simply be
    "copy *.py": Clock ships its chime sounds inside its package.
    """

    parts = relpath.split("/")
    if any(part in EXCLUDED_NAMES or part.startswith(".") for part in parts):
        return False
    if relpath.endswith(EXCLUDED_SUFFIXES):
        return False
    return parts[0] not in extra and relpath not in extra


class Installer:
    """Runs one update at a time, start to finish or not at all."""

    def __init__(
        self,
        extensions_dir: str,
        log: Callable[[str], None] | None = None,
        self_source_id: str = "",
    ) -> None:
        self._extensions_dir = extensions_dir
        self._log = log or (lambda _message: None)
        # Our own source id, so updating ourselves can be told apart from
        # updating anything else. Reloading the extension that is running the
        # reload would re-import this module underneath its own stack frames.
        self._self_source_id = self_source_id
        self._client: Client | None = None
        self._cancelled = False

    def cancel(self) -> None:
        """Abandons an update in flight."""

        self._cancelled = True
        if self._client is not None:
            self._client.cancel()

    # --- The one public entry point --------------------------------------

    def update(
        self,
        item: Managed,
        tag: str,
        on_progress: Callable[[float], None],
        on_done: Callable[[Result], None],
        expected_bytes: int = 0,
    ) -> None:
        """Updates `item` to `tag`, reporting progress as a 0-1 fraction."""

        result = Result(
            label=item.label,
            source_id=item.source_id,
            from_version=item.version,
            to_version=tag.lstrip("v"),
        )

        try:
            workdir = tempfile.mkdtemp(prefix="orca-extension-update-")
        except OSError as error:
            result.error = f"could not create a working directory: {error}"
            on_done(result)
            return

        tarball = os.path.join(workdir, "release.tar.gz")
        self._client = Client()

        def progress(fraction: float) -> None:
            on_progress(fraction * DOWNLOAD_END)

        def downloaded(ok: bool, error: str | None, size: int) -> None:
            result.downloaded_bytes = size
            if not ok:
                shutil.rmtree(workdir, ignore_errors=True)
                result.error = error or "the download failed"
                on_done(result)
                return
            # Hand the main loop a turn so the bar reaches 50% visibly before
            # the local work begins.
            GLib.idle_add(self._install, item, tarball, workdir, result, on_progress, on_done)

        self._client.download_tarball(
            item.repo, tag, tarball, progress, downloaded, expected_bytes
        )

    # --- Everything after the download -----------------------------------

    def _install(
        self,
        item: Managed,
        tarball: str,
        workdir: str,
        result: Result,
        on_progress: Callable[[float], None],
        on_done: Callable[[Result], None],
    ) -> bool:
        paths = _Paths(
            installed=os.path.join(self._extensions_dir, item.source_id),
            staging=os.path.join(data_dir(), "staging", item.source_id),
            backup=os.path.join(data_dir(), "backups", item.source_id),
            workdir=workdir,
        )
        try:
            self._run(item, tarball, paths, result, on_progress)
        except _Failed as failure:
            result.error = str(failure)
            result.unrecoverable = failure.unrecoverable
            self._restore(item, paths, result)
        except Exception as error:  # pylint: disable=broad-exception-caught
            result.error = f"unexpected failure: {error}"
            self._restore(item, paths, result)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            shutil.rmtree(paths.staging, ignore_errors=True)
            self._client = None

        on_done(result)
        return False

    def _run(
        self,
        item: Managed,
        tarball: str,
        paths: _Paths,
        result: Result,
        on_progress: Callable[[float], None],
    ) -> None:
        extracted = os.path.join(paths.workdir, "extracted")
        self._extract(tarball, extracted)
        on_progress(DOWNLOAD_END + (VALIDATE_END - DOWNLOAD_END) * 0.4)

        candidate = self._find_package(extracted, item)
        result.has_install_script = bool(
            glob.glob(os.path.join(extracted, "*", "install.sh"))
        )

        self._validate(candidate, on_progress)
        on_progress(VALIDATE_END)

        self._stage(candidate, paths.staging, os.path.dirname(candidate))
        on_progress(VALIDATE_END + (1.0 - VALIDATE_END) * 0.3)

        self._swap(paths)
        on_progress(VALIDATE_END + (1.0 - VALIDATE_END) * 0.6)

        self._approve(paths.installed)
        self._reload(item, result)
        on_progress(1.0)
        result.ok = True

    def _extract(self, tarball: str, destination: str) -> None:
        os.makedirs(destination, exist_ok=True)
        try:
            with tarfile.open(tarball, "r:*") as archive:
                # filter="data" refuses absolute paths, parent traversal,
                # device nodes, symlinks pointing outside the tree and
                # anything else that would write beyond `destination`. This
                # is an archive off the network; it is data, not something to
                # be trusted with a path.
                archive.extractall(destination, filter="data")
        except (tarfile.TarError, OSError, ValueError) as error:
            raise _Failed(f"the release archive could not be read: {error}") from error

    def _find_package(self, extracted: str, item: Managed) -> str:
        # A GitHub tarball holds a single top-level directory named after the
        # owner, repo and commit, so the package sits one level down. We know
        # its name: we are updating something already installed.
        matches = sorted(glob.glob(os.path.join(extracted, "*", item.source_id)))
        for match in matches:
            if item.is_package and os.path.isfile(os.path.join(match, "__init__.py")):
                return match
            if not item.is_package and os.path.isfile(match):
                return match
        raise _Failed(
            f"the release does not contain '{item.source_id}' where the installed "
            "version has it"
        )

    def _validate(self, candidate: str, on_progress: Callable[[float], None]) -> None:
        """Compiles every Python file. A syntax error stops here."""

        if os.path.isfile(candidate):
            sources = [candidate]
        else:
            sources = sorted(
                os.path.join(root, name)
                for root, _dirs, names in os.walk(candidate)
                for name in names
                if name.endswith(".py")
            )
        if not sources:
            raise _Failed("the release contains no Python files")

        span = VALIDATE_END - (DOWNLOAD_END + (VALIDATE_END - DOWNLOAD_END) * 0.4)
        base = DOWNLOAD_END + (VALIDATE_END - DOWNLOAD_END) * 0.4
        for index, source in enumerate(sources, start=1):
            try:
                with open(source, encoding="utf-8") as handle:
                    text = handle.read()
                # compile(), not py_compile: this checks the syntax without
                # writing a .pyc into a tree we are about to hash.
                compile(text, source, "exec")
            except (OSError, UnicodeDecodeError) as error:
                raise _Failed(f"{os.path.basename(source)} could not be read: {error}") from error
            except SyntaxError as error:
                raise _Failed(
                    f"{os.path.basename(source)} has a syntax error at line "
                    f"{error.lineno}: {error.msg}"
                ) from error
            on_progress(base + span * index / len(sources))

    def _stage(self, candidate: str, staging: str, repo_root: str = "") -> None:
        """Builds the exact tree that will be installed, beside the target.

        Built here rather than written straight into the extensions
        directory because Orca enumerates that directory at startup without
        a guard, so a half-written package in it is a startup hazard. Staging
        lives on the same filesystem as the target so the swap is a rename.
        """

        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(os.path.dirname(staging), exist_ok=True)

        if os.path.isfile(candidate):
            shutil.copy2(candidate, staging)
            return

        extra = read_update_exclude(candidate)
        os.makedirs(staging, exist_ok=True)
        copied = 0
        for root, dirs, names in os.walk(candidate):
            rel_root = os.path.relpath(root, candidate)
            rel_root = "" if rel_root == "." else rel_root.replace(os.sep, "/")
            dirs[:] = [
                d for d in sorted(dirs)
                if should_copy(f"{rel_root}/{d}".lstrip("/"), extra)
            ]
            for name in sorted(names):
                relpath = f"{rel_root}/{name}".lstrip("/")
                if not should_copy(relpath, extra):
                    continue
                target = os.path.join(staging, relpath)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(os.path.join(root, name), target)
                copied += 1

        copied += self._stage_included(candidate, staging, repo_root, extra)
        if not copied:
            raise _Failed("the release package is empty once filtered")

    def _stage_included(
        self, candidate: str, staging: str, repo_root: str, extra: tuple[str, ...]
    ) -> int:
        """Copies repository-root paths the package asks to have installed."""

        if not repo_root:
            return 0
        copied = 0
        for name in read_update_include(candidate):
            source = os.path.join(repo_root, name)
            if not os.path.exists(source):
                continue
            if os.path.isfile(source):
                if should_copy(name, extra):
                    shutil.copy2(source, os.path.join(staging, name))
                    copied += 1
                continue
            for root, dirs, names in os.walk(source):
                rel_root = os.path.relpath(root, source)
                prefix = name if rel_root == "." else f"{name}/{rel_root.replace(os.sep, '/')}"
                dirs[:] = [d for d in sorted(dirs) if should_copy(f"{prefix}/{d}", extra)]
                for entry in sorted(names):
                    relpath = f"{prefix}/{entry}"
                    if not should_copy(relpath, extra):
                        continue
                    target = os.path.join(staging, relpath)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(os.path.join(root, entry), target)
                    copied += 1
        return copied

    def _swap(self, paths: _Paths) -> None:
        """Moves the installed version to the backup and the staged one in."""

        os.makedirs(os.path.dirname(paths.backup), exist_ok=True)
        shutil.rmtree(paths.backup, ignore_errors=True)
        if os.path.exists(paths.backup):
            os.remove(paths.backup)

        try:
            if os.path.exists(paths.installed):
                os.rename(paths.installed, paths.backup)
            os.rename(paths.staging, paths.installed)
        except OSError as error:
            raise _Failed(f"could not install the update: {error}") from error

    def _approve(self, filepath: str) -> None:
        from orca import extension_loader

        try:
            extension_loader.get_loader().approve_extension_file(filepath)
        except Exception as error:  # pylint: disable=broad-exception-caught
            raise _Failed(f"could not approve the update: {error}") from error

    def _reload(self, item: Managed, result: Result | None = None) -> None:
        """Loads the new version now, so a bad one is caught while we can undo it.

        Two checks, because they catch different things. The try/except
        catches a raising ``_get_commands()``, which Orca does not guard and
        which would otherwise break the *next* startup. The lookup afterwards
        catches a failed import or a raising constructor, which Orca does
        guard -- it logs them and returns None, so the extension simply is
        not there and nothing says so out loud.

        **Except for ourselves.** Reloading this extension would purge this
        very module from ``sys.modules`` and re-import it while the code doing
        the reloading is still running inside the old one, and Orca's reload
        also tears down and re-registers every extension's commands on the
        way through. The new version is installed and approved, and a restart
        picks it up; that is reported rather than risked.
        """

        from orca import extension_loader

        if self._self_source_id and item.source_id == self._self_source_id:
            self._log(
                f"{item.source_id} is this extension; installed and approved but "
                "not reloaded. A restart is needed to run the new version."
            )
            if result is not None:
                result.restart_required = True
            return

        loader = extension_loader.get_loader()
        try:
            loader.reload_user_extension(self._extensions_dir, item.source_id)
        except Exception as error:  # pylint: disable=broad-exception-caught
            raise _Failed(f"the new version failed to load: {error}") from error

        if item.class_name and loader.get_loaded_user_extension(item.class_name) is None:
            raise _Failed("the new version did not load")

    # --- Going back ------------------------------------------------------

    def _restore(self, item: Managed, paths: _Paths, result: Result) -> None:
        """Puts the previous version back and proves it still runs."""

        if not os.path.exists(paths.backup):
            # Nothing was moved, so the installed version was never disturbed.
            return

        self._log(f"restoring {item.source_id} after: {result.error}")
        try:
            if os.path.exists(paths.installed):
                shutil.rmtree(paths.installed, ignore_errors=True)
                if os.path.exists(paths.installed):
                    os.remove(paths.installed)
            os.rename(paths.backup, paths.installed)
            self._approve(paths.installed)
            self._reload(item)
        except Exception as error:  # pylint: disable=broad-exception-caught
            result.unrecoverable = True
            result.error = (
                f"{result.error}; and the previous version could not be put back "
                f"({error}). Restart Orca."
            )
            self._log(f"ROLLBACK FAILED for {item.source_id}: {error}")
            return
        self._log(f"restored {item.source_id} to {item.version}")


class _Failed(Exception):
    """An update step failed. The message is user-facing."""

    def __init__(self, message: str, unrecoverable: bool = False) -> None:
        super().__init__(message)
        self.unrecoverable = unrecoverable
