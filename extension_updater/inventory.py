"""What is installed, where it came from, and whether it is out of date.

Everything here is a read. Nothing in this module imports an extension,
downloads anything or touches the filesystem outside Orca's own directories --
the whole inventory comes from ``extension_loader.discover_user_extensions``,
which parses each extension's metadata with ``ast`` and never executes it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# A GitHub project URL and nothing else. Two path segments exactly: an owner
# and a repository. Anything deeper is a link to a file or a wiki page, and
# anything shallower is a user profile -- neither names a release feed, so
# neither is accepted. A trailing slash or a .git suffix is tolerated because
# both are things people paste.
_GITHUB_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$"
)

# A release tag we can compare. Orca's VERSION strings are bare ("2.10.0")
# and GitHub's tags are prefixed ("v2.10.0"), so the v is optional. A tag
# that does not match is not an error; it is simply not a version we can
# reason about, and is skipped rather than guessed at.
_VERSION = re.compile(r"^v?(?P<digits>[0-9]+(?:\.[0-9]+)*)$")


@dataclass(frozen=True)
class Managed:
    """An installed extension that declares a GitHub repository."""

    source_id: str          # the approval key: "polyglot", or "foo.py"
    class_name: str         # the Extension subclass name, for reload bookkeeping
    label: str              # GROUP_LABEL, what the user calls it
    version: str            # installed version, as declared
    repo: str               # "owner/repo"
    filepath: str
    is_package: bool


@dataclass(frozen=True)
class Unmanaged:
    """An installed extension the updater cannot check, and why."""

    source_id: str
    label: str
    version: str
    reason: str


def extensions_dir() -> str:
    """Returns Orca's user-extensions directory.

    The loader takes this as an argument and exposes no getter for it, so
    every caller computes it the same way; this is Orca's own expression,
    from /usr/bin/orca.
    """

    from gi.repository import GLib

    return os.path.join(GLib.get_user_data_dir(), "orca", "extensions")


def parse_repo(website: str) -> str | None:
    """Returns "owner/repo" from an extension's WEBSITE, or None."""

    if not website:
        return None
    match = _GITHUB_URL.match(website.strip())
    if match is None:
        return None
    repo = match.group("repo")
    # "owner/." and "owner/.." would resolve to directories, not repositories,
    # and this string is about to be interpolated into an API path.
    if repo in (".", ".."):
        return None
    return f"{match.group('owner')}/{repo}"


def parse_version(text: str) -> tuple[int, ...] | None:
    """Returns a comparable version tuple, or None if it is not one.

    Trailing zeros are significant to nobody, so they are stripped: 2.1 and
    2.1.0 are the same release, and without this a remote "v2.1" would read
    as older than an installed "2.1.0" and be skipped forever.
    """

    if not text:
        return None
    match = _VERSION.match(text.strip())
    if match is None:
        return None
    parts = [int(p) for p in match.group("digits").split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(remote: str, installed: str) -> bool:
    """Returns True only if `remote` is a strictly higher version.

    False for equal versions, for a remote that is *lower* -- a rolled-back
    tag must not drag a working install backwards -- and for anything either
    side cannot be parsed, because an unknown version is not evidence of an
    update.
    """

    remote_parts = parse_version(remote)
    installed_parts = parse_version(installed)
    if remote_parts is None or installed_parts is None:
        return False
    return remote_parts > installed_parts


def discover(extensions: str | None = None) -> tuple[list[Managed], list[Unmanaged]]:
    """Returns the installed extensions, split by whether we can check them."""

    from orca import extension_loader

    directory = extensions if extensions is not None else extensions_dir()
    managed: list[Managed] = []
    unmanaged: list[Unmanaged] = []

    for info in extension_loader.get_loader().discover_user_extensions(directory):
        label = info.group_label or info.class_name or info.filename
        repo = parse_repo(info.website)
        if repo is None:
            reason = (
                "declares no repository"
                if not info.website
                else f"repository is not a GitHub project: {info.website}"
            )
            unmanaged.append(Unmanaged(info.filename, label, info.version, reason))
            continue
        if parse_version(info.version) is None:
            unmanaged.append(
                Unmanaged(
                    info.filename,
                    label,
                    info.version,
                    "declares no comparable version"
                    if not info.version
                    else f"version is not comparable: {info.version}",
                )
            )
            continue
        managed.append(
            Managed(
                source_id=info.filename,
                class_name=info.class_name or "",
                label=label,
                version=info.version,
                repo=repo,
                filepath=info.filepath,
                is_package=info.is_package,
            )
        )

    return managed, unmanaged
