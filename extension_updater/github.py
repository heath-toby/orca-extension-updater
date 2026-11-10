"""Asynchronous GitHub release lookups and tarball downloads.

Every call here returns immediately and reports its result through a callback
on the GLib main loop. That is the whole point of the module and it is not a
stylistic preference: this add-on runs inside Orca, and two separate bugs in
this family of add-ons were a blocking call made on Orca's main loop -- a
nine-second clipboard read and a forty-nine-second braille table change, both
of which froze speech outright. A network call is more likely to stall than
either, and orca.service ships WatchdogSec=10, so a blocking download would
not merely stall speech but have systemd kill Orca mid-update.

libsoup does the work on its own threads and hands the result back on the main
context, so no worker thread of ours is involved.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import gi

gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, Soup  # noqa: E402

API = "https://api.github.com"

# Identifies this client and nothing else. Deliberately carries no address:
# a service asking automated clients for contact details is not a reason to
# hand over the user's email.
USER_AGENT = "orca-extension-updater"

# A stalled connection must fail, not hang. libsoup's own timeouts cover the
# connect and the idle gap; the deadline below covers the whole operation,
# including a server that dribbles bytes forever without ever going idle.
_CONNECT_TIMEOUT_SECONDS = 15
_IDLE_TIMEOUT_SECONDS = 20
_REQUEST_DEADLINE_SECONDS = 60
_DOWNLOAD_DEADLINE_SECONDS = 300

_CHUNK = 64 * 1024

# How long a release tag stays usable without asking again.
#
# This exists because of how the allowance is actually spent: a check costs
# one request per add-on, and a round of updates costs one more each, so with
# ten add-ons a check followed by updating them all is nineteen of the sixty
# requests an hour allows. Retry that twice and the last add-on in the run
# fails for want of a request -- which is exactly what happened in the field,
# always to the last one alphabetically.
#
# Fifteen minutes is chosen against the shortest check interval, an hour: a
# scheduled check never finds the cache warm, so this only ever saves the
# requests spent by pressing the key again, which is the case that was
# exhausting the allowance.
TAG_CACHE_SECONDS = 900

_tag_cache: dict[str, tuple[str, float]] = {}


def clear_tag_cache() -> None:
    """Forgets every remembered tag. For tests, and for a forced re-check."""

    _tag_cache.clear()


def cached_tag(repo: str) -> str | None:
    """Returns the tag last seen for `repo`, if it is still fresh."""

    entry = _tag_cache.get(repo)
    if entry is None:
        return None
    tag, when = entry
    if time.time() - when > TAG_CACHE_SECONDS:
        del _tag_cache[repo]
        return None
    return tag


# What to assume a tarball weighs when we have never fetched this repository
# before. The add-ons in this family run from roughly 30 KB to 160 KB, so this
# is a little above the largest of them: a guess that is too high makes the bar
# lag and then jump at the end, which is better than one that is too low and
# sits pinned at the ceiling for most of the download.
DEFAULT_ESTIMATE_BYTES = 192 * 1024

# How far an estimated fraction may advance before the stream actually ends.
_ESTIMATE_CEILING = 0.98

# A tarball for one of these add-ons is tens of kilobytes. The cap is not
# about disk, it is about not reading an unbounded stream into memory inside
# the screen reader's own process because a URL pointed somewhere unexpected.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class Error(Exception):
    """A lookup or download failed. The message is user-facing."""


# What GitHub last said about the hourly allowance. Every response carries it,
# so tracking it costs nothing -- and a request for /rate_limit would itself
# be spending the thing it asks about.
_budget: dict[str, int | None] = {"remaining": None, "reset": None}


def budget() -> tuple[int | None, int | None]:
    """Returns (requests remaining, reset time), or (None, None) if unknown."""

    return _budget["remaining"], _budget["reset"]


def _note_budget(message: Soup.Message) -> None:
    """Records the allowance from a response's headers."""

    headers = message.get_response_headers()
    for header, key in (("x-ratelimit-remaining", "remaining"), ("x-ratelimit-reset", "reset")):
        raw = headers.get_one(header)
        if raw is None:
            continue
        try:
            _budget[key] = int(raw)
        except ValueError:
            pass


def _exhausted() -> bool:
    """Returns True when the allowance is known to be spent and not yet reset."""

    remaining, reset = budget()
    if remaining is None or remaining > 0:
        return False
    return reset is None or reset > time.time()


def rate_limit_message() -> str:
    """Says the allowance is spent, and when it comes back.

    Worth the extra clause: without the reset time, "the rate limit is
    exhausted" reads like a fault to investigate rather than something that
    fixes itself shortly. With nine add-ons, a check plus a round of updates
    is a third of the hourly allowance, so this is a state users will meet.
    """

    _remaining, reset = budget()
    if reset:
        minutes = int((reset - time.time()) // 60) + 1
        if minutes > 0:
            plural = "" if minutes == 1 else "s"
            return (
                "GitHub's hourly limit of 60 requests is used up; it resets in "
                f"{minutes} minute{plural}"
            )
    return "GitHub's hourly limit of 60 requests is used up"


def _rate_limited(message: Soup.Message) -> bool:
    """Returns True when this response was refused for the allowance."""

    status = int(message.get_status())
    if status not in (403, 429):
        return False
    remaining = message.get_response_headers().get_one("x-ratelimit-remaining")
    return remaining == "0"


def _session() -> Soup.Session:
    session = Soup.Session.new()
    session.set_user_agent(USER_AGENT)
    session.set_timeout(_CONNECT_TIMEOUT_SECONDS)
    session.set_idle_timeout(_IDLE_TIMEOUT_SECONDS)
    return session


def _deadline(cancellable: Gio.Cancellable, seconds: int) -> int:
    """Cancels `cancellable` after `seconds`. Returns the timeout source id."""

    def fire() -> bool:
        cancellable.cancel()
        return False

    return GLib.timeout_add_seconds(seconds, fire)


class Client:
    """Issues the API calls this add-on needs, one at a time per instance."""

    def __init__(self) -> None:
        self._session = _session()
        self._cancellable: Gio.Cancellable | None = None
        self._deadline_id: int = 0

    def cancel(self) -> None:
        """Abandons whatever is in flight. Safe to call when nothing is."""

        if self._deadline_id:
            GLib.source_remove(self._deadline_id)
            self._deadline_id = 0
        if self._cancellable is not None:
            self._cancellable.cancel()
            self._cancellable = None
        # Dropping a session with connections still open logs a runtime
        # warning from libsoup; abort closes them first.
        self._session.abort()

    def _clear_deadline(self) -> None:
        if self._deadline_id:
            GLib.source_remove(self._deadline_id)
            self._deadline_id = 0

    # --- JSON ------------------------------------------------------------

    def _get_json(
        self,
        path: str,
        on_done: Callable[[object | None, str | None], None],
    ) -> None:
        """GETs `path` and hands the decoded body, or an error string, to on_done."""

        if _exhausted():
            on_done(None, rate_limit_message())
            return

        message = Soup.Message.new("GET", f"{API}{path}")
        if message is None:
            on_done(None, f"Could not build a request for {path}.")
            return
        headers = message.get_request_headers()
        headers.append("Accept", "application/vnd.github+json")
        headers.append("X-GitHub-Api-Version", "2022-11-28")

        cancellable = Gio.Cancellable.new()
        self._cancellable = cancellable
        self._deadline_id = _deadline(cancellable, _REQUEST_DEADLINE_SECONDS)

        def finished(session: Soup.Session, result: Gio.AsyncResult) -> None:
            self._clear_deadline()
            try:
                body = session.send_and_read_finish(result)
            except GLib.Error as error:
                on_done(None, _describe(error))
                return

            _note_budget(message)
            status = message.get_status()
            if status == Soup.Status.NOT_FOUND:
                on_done(None, "not found")
                return
            if _rate_limited(message):
                on_done(None, rate_limit_message())
                return
            if status == Soup.Status.FORBIDDEN or status == 429:
                on_done(None, f"GitHub refused the request ({int(status)}).")
                return
            if status != Soup.Status.OK:
                on_done(None, f"GitHub returned {int(status)}.")
                return

            data = body.get_data() if body is not None else None
            if not data:
                on_done(None, "GitHub returned an empty response.")
                return
            try:
                on_done(json.loads(data.decode("utf-8")), None)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                on_done(None, f"GitHub returned something that is not JSON: {error}")

        self._session.send_and_read_async(
            message, GLib.PRIORITY_DEFAULT, cancellable, finished
        )

    def latest_version(
        self,
        repo: str,
        on_done: Callable[[str | None, str | None], None],
    ) -> None:
        """Finds the newest release tag for `repo`, falling back to tags.

        Not every repository publishes releases -- orca-signal-filter has
        none -- and a repository with tags but no releases is still one we
        can update from, so a 404 on the releases endpoint falls through to
        the tag list rather than reporting failure.
        """

        remembered = cached_tag(repo)
        if remembered is not None:
            # Answer through the main loop rather than straight away, so every
            # caller sees the same asynchronous shape whether or not the tag
            # was already known.
            GLib.idle_add(lambda: (on_done(remembered, None), False)[1])
            return

        def releases_done(payload: object | None, error: str | None) -> None:
            if error == "not found":
                self._tags(repo, on_done)
                return
            if error is not None:
                on_done(None, error)
                return
            tag = payload.get("tag_name") if isinstance(payload, dict) else None
            if not tag:
                self._tags(repo, on_done)
                return
            _tag_cache[repo] = (tag, time.time())
            on_done(tag, None)

        self._get_json(f"/repos/{repo}/releases/latest", releases_done)

    def _tags(
        self,
        repo: str,
        on_done: Callable[[str | None, str | None], None],
    ) -> None:
        from .inventory import parse_version

        def tags_done(payload: object | None, error: str | None) -> None:
            if error is not None:
                on_done(None, "the repository has no releases" if error == "not found" else error)
                return
            if not isinstance(payload, list):
                on_done(None, "GitHub returned an unexpected tag list.")
                return
            best_tag, best_parts = None, None
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not isinstance(name, str):
                    continue
                parts = parse_version(name)
                if parts is None:
                    continue
                if best_parts is None or parts > best_parts:
                    best_tag, best_parts = name, parts
            if best_tag is None:
                on_done(None, "the repository has no releases or version tags")
                return
            _tag_cache[repo] = (best_tag, time.time())
            on_done(best_tag, None)

        self._get_json(f"/repos/{repo}/tags?per_page=100", tags_done)

    # --- Download --------------------------------------------------------

    def download_tarball(
        self,
        repo: str,
        tag: str,
        destination: str,
        on_progress: Callable[[float], None],
        on_done: Callable[[bool, str | None, int], None],
        expected_bytes: int = 0,
    ) -> None:
        """Streams a release tarball to `destination`.

        Releases for these add-ons carry no assets, only the auto-generated
        tarball, so that is what is fetched.

        **A Content-Length may or may not arrive, and the first fetch of any
        tag is the one without it.** Measured on two tags rather than assumed:
        the API redirects to codeload.github.com, which generates the archive
        on the first request and signals the end by closing the connection
        (``Soup.Encoding.EOF``, no length); once it has that archive cached it
        serves the same URL with a real Content-Length
        (``Soup.Encoding.CONTENT_LENGTH``). A new release is therefore exactly
        the case where the size is unknown, so the unknown path is the normal
        one and not an edge case.

        With a length, the fraction is bytes over it. Without, it is bytes
        over `expected_bytes` -- the size this repository's tarball was last
        time, which the caller remembers -- or DEFAULT_ESTIMATE_BYTES when
        there is no last time. Because that is a guess, an estimated fraction
        is held below 1.0 until the stream actually ends: the bar moves with
        real bytes received and never claims to have finished before it has.
        `on_done` reports the true size so the caller can remember it.
        """

        if _exhausted():
            on_done(False, rate_limit_message(), 0)
            return

        message = Soup.Message.new("GET", f"{API}/repos/{repo}/tarball/{tag}")
        if message is None:
            on_done(False, "Could not build a download request.", 0)
            return
        message.get_request_headers().append("Accept", "application/vnd.github+json")

        cancellable = Gio.Cancellable.new()
        self._cancellable = cancellable
        self._deadline_id = _deadline(cancellable, _DOWNLOAD_DEADLINE_SECONDS)

        state = {"received": 0, "total": 0, "measured": False}

        def fail(reason: str) -> None:
            self._clear_deadline()
            on_done(False, reason, state["received"])

        def sent(session: Soup.Session, result: Gio.AsyncResult) -> None:
            try:
                stream = session.send_finish(result)
            except GLib.Error as error:
                fail(_describe(error))
                return

            _note_budget(message)
            if _rate_limited(message):
                # This is the branch that reported a bare "403" and left the
                # user with no idea the fix was simply to wait.
                fail(rate_limit_message())
                return
            if message.get_status() != Soup.Status.OK:
                fail(f"GitHub returned {int(message.get_status())} for the download.")
                return

            length = message.get_response_headers().get_content_length()
            if length and length > 0:
                state["total"], state["measured"] = length, True
            else:
                state["total"] = expected_bytes or DEFAULT_ESTIMATE_BYTES
            if state["measured"] and state["total"] > MAX_DOWNLOAD_BYTES:
                fail("The download is larger than this add-on is willing to fetch.")
                return

            try:
                sink = Gio.File.new_for_path(destination).replace(
                    None, False, Gio.FileCreateFlags.PRIVATE, cancellable
                )
            except GLib.Error as error:
                fail(_describe(error))
                return

            pump(stream, sink)

        def pump(stream: Gio.InputStream, sink: Gio.OutputStream) -> None:
            def chunk_read(source: Gio.InputStream, result: Gio.AsyncResult) -> None:
                try:
                    data = source.read_bytes_finish(result)
                except GLib.Error as error:
                    _close(stream, sink)
                    fail(_describe(error))
                    return

                size = data.get_size() if data is not None else 0
                if size == 0:
                    _close(stream, sink)
                    self._clear_deadline()
                    on_progress(1.0)
                    on_done(True, None, state["received"])
                    return

                state["received"] += size
                if state["received"] > MAX_DOWNLOAD_BYTES:
                    _close(stream, sink)
                    fail("The download exceeded the size this add-on will fetch.")
                    return

                try:
                    sink.write_bytes(data, cancellable)
                except GLib.Error as error:
                    _close(stream, sink)
                    fail(_describe(error))
                    return

                if state["total"]:
                    fraction = state["received"] / state["total"]
                    # An estimate must not reach the end of the phase; only
                    # the stream closing may do that.
                    ceiling = 1.0 if state["measured"] else _ESTIMATE_CEILING
                    on_progress(min(ceiling, fraction))

                source.read_bytes_async(
                    _CHUNK, GLib.PRIORITY_DEFAULT, cancellable, chunk_read
                )

            stream.read_bytes_async(
                _CHUNK, GLib.PRIORITY_DEFAULT, cancellable, chunk_read
            )

        self._session.send_async(message, GLib.PRIORITY_DEFAULT, cancellable, sent)


def _close(stream: Gio.InputStream, sink: Gio.OutputStream) -> None:
    for closeable in (sink, stream):
        try:
            closeable.close(None)
        except GLib.Error:
            pass


def _describe(error: GLib.Error) -> str:
    """Turns a GLib.Error into something worth speaking."""

    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
        return "the request timed out"
    return error.message or "the request failed"
