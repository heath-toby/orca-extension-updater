#!/usr/bin/env python3
"""Reports which installed Orca extensions can actually be imported.

    ./tools/probe-extensions.py

**Why this exists.** Orca catches a failed extension import, logs it at
WARNING in the debug log, and carries on starting. There is no speech, no
braille, no notification, and nothing in Preferences that says the extension
is absent rather than quiet -- it simply is not there. An add-on can therefore
stop working for weeks and the only symptom is the thing it used to do no
longer happening.

That is not hypothetical. Orca renamed `orca.clipboard` to
`orca.clipboard_manager`, and Wayland Clipboard imported the old name; it had
not loaded since, and nothing said so. This script would have found it in a
second.

It imports each extension's module, which runs that module's top-level code --
the same code Orca runs when it loads it. It does **not** construct the
Extension subclass, so no hooks run and nothing is patched into a live Orca.
Run it in a terminal, not inside Orca.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import orca.extension_loader  # noqa: F401  (import order: see install.sh)

from gi.repository import GLib  # noqa: E402

EXTENSIONS = os.path.join(GLib.get_user_data_dir(), "orca", "extensions")


def sources(directory: str) -> list[tuple[str, str, str | None]]:
    """Returns (source id, file to import, package directory) per extension.

    The same rules Orca's loader uses: skip anything beginning with an
    underscore, accept a .py file or a directory holding __init__.py.
    """

    found: list[tuple[str, str, str | None]] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError as error:
        print(f"Cannot read {directory}: {error}", file=sys.stderr)
        return found

    for name in names:
        if name.startswith("_"):
            continue
        path = os.path.join(directory, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "__init__.py")):
            found.append((name, os.path.join(path, "__init__.py"), path))
        elif name.endswith(".py"):
            found.append((name, path, None))
    return found


def main() -> int:
    # Orca's loader imports extensions under this synthetic parent but never
    # creates it, so a probe has to create it for the relative imports inside
    # a package to resolve.
    parent = sys.modules.setdefault(
        "orca_user_extension", types.ModuleType("orca_user_extension")
    )
    if not hasattr(parent, "__path__"):
        parent.__path__ = []  # type: ignore[attr-defined]

    entries = sources(EXTENSIONS)
    if not entries:
        print(f"No extensions found in {EXTENSIONS}")
        return 0

    width = max(len(source_id) for source_id, _path, _pkg in entries)
    broken = []

    for source_id, path, package_dir in entries:
        namespace = source_id[:-3] if source_id.endswith(".py") else source_id
        # A distinct name, so probing cannot disturb anything Orca has loaded
        # if this is ever run in the same process as a live Orca.
        module_name = f"orca_user_extension.probe_{namespace}"
        spec = importlib.util.spec_from_file_location(
            module_name,
            path,
            submodule_search_locations=[package_dir] if package_dir else None,
        )
        if spec is None or spec.loader is None:
            broken.append((source_id, "no import machinery for this file"))
            print(f"  BROKEN  {source_id:<{width}}  no import machinery")
            continue
        module = importlib.util.module_from_spec(spec)
        # In sys.modules before exec_module, so code run during class creation
        # (a dataclass, for instance) can look itself up.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as error:  # pylint: disable=broad-exception-caught
            reason = f"{type(error).__name__}: {error}"
            broken.append((source_id, reason))
            print(f"  BROKEN  {source_id:<{width}}  {reason}")
        else:
            print(f"  ok      {source_id}")

    print()
    if broken:
        print(f"{len(entries) - len(broken)}/{len(entries)} import; {len(broken)} do not:")
        for source_id, reason in broken:
            print(f"  {source_id}: {reason}")
        print()
        print("An extension that does not import is not loaded by Orca, and Orca")
        print("says nothing about it. Fix the import, then re-run its install.sh.")
        return 1
    print(f"All {len(entries)} installed extensions import cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
