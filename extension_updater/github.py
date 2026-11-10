"""Finding releases and fetching them, without spending GitHub's API allowance.

Three channels exist and this module deliberately prefers the two that are
not rationed:

1. **The ``gh`` CLI**, when it is installed and signed in. It uses the user's
   own token, which is allowed 5,000 requests an hour rather than 60, and it
   is the only channel that honours GitHub's own idea of the *latest* release
   (drafts and pre-releases excluded).
2. **The releases and tags Atom feeds** on github.com. These are not API
   endpoints: they carry no ``x-ratelimit`` headers and spend no allowance at
   all. Measured, not assumed -- with the unauthenticated API returning 403
   for exhaustion, ``releases.atom`` still returned 200.
3. **The REST API is not used.** Sixty requests an hour cannot support ten
   add-ons checked and updated more than twice, which is how this add-on came
   to fail on whichever extension sorted last.

Downloads go to ``github.com/<repo>/archive/refs/tags/<tag>.tar.gz`` for the
same reason: that is codeload, not the API, and it answered 200 while the API
was refusing everything.

Every call returns immediately and reports through a callback on the GLib main
loop. That is not a style choice: this runs inside Orca, where two separate
bugs in this family of add-ons were a blocking call on the main loop, and
``orca.service`` ships ``WatchdogSec=10``.
"""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Callable, Sequence

import gi

gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, Soup  # noqa: E402

GITHUB = "https://github.com"

# Identifies this client and nothing else. Deliberately carries no address: a
# service asking automated clients for contact details is not a reason to hand
# over the user's email.
USER_AGENT = "orca-extension-updater"

_CONNECT_TIMEOUT_SECONDS = 15
_IDLE_TIMEOUT_SECONDS = 20
_REQUEST_DEADLINE_SECONDS = 60
_DOWNLOAD_DEADLINE_SECONDS = 300
_GH_DEADLINE_SECONDS = 30

_CHUNK = 64 * 1024

# What to assume a tarball weighs when we have never fetched this repository
# before. The add-ons in this family run from roughly 15 KB to 2.5 MB, so this
# is a middling guess: too high makes the bar lag then jump, too low pins it at
# the ceiling. The real size is remembered after the first download.
DEFAULT_ESTIMATE_BYTES = 192 * 1024

# How far an estimated fraction may advance before the stream actually ends.
_ESTIMATE_CEILING = 0.98

# A tarball for one of these add-ons is kilobytes to a couple of megabytes.
# The cap is not about disk: it is about not reading an unbounded stream into
# the screen reader's own process because a URL pointed somewhere unexpected.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024

# How long a release tag stays usable without asking again. Fifteen minutes is
# measured against the shortest check interval, an hour, so a scheduled check
# never answers from the cache; it only saves the requests spent by pressing
# the key again.
TAG_CACHE_SECONDS = 900

# Tag names appear in both feeds as .../releases/tag/<tag>.
_FEED_TAG = re.compile(r"/releases/tag/([^\"'<>\s]+)")

_tag_cache: dict[str, tuple[str, float]] = {}


class Error(Exception):
    """A lookup or download failed. The message is user-facing."""


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


def gh_path() -> str | None:
    """Returns the path to the gh CLI, or None if it is not installed."""

    return shutil.which("gh")


def rate_limit_message() -> str:
    """Says github.com is refusing requests for volume, in plain terms."""

    return (
        "GitHub is refusing requests for the moment because too many have been "
        "made. Try again shortly"
    )


def _rate_limited(message: Soup.Message) -> bool:
    """Returns True when this response was refused for request volume."""

    return int(message.get_status()) in (403, 429)


def best_tag(candidates: Sequence[str]) -> str | None:
    """Returns the highest version-like tag from `candidates`.

    The feeds list newest first and in practice that matches a version sort,
    but sorting by version is what the answer actually depends on, so it is
    what is done. Anything that is not a plain ``1.2.3`` -- a date tag, a code
    name, a release candidate -- is skipped rather than guessed at.
    """

    from .inventory import parse_version

    best, best_parts = None, None
    for candidate in candidates:
        parts = parse_version(candidate)
        if parts is None:
            continue
        if best_parts is None or parts > best_parts:
            best, best_parts = candidate, parts
    return best


def parse_feed_tags(xml: str) -> list[str]:
    """Returns the tag names named by a releases or tags Atom feed."""

    seen, tags = set(), []
    for tag in _FEED_TAG.findall(xml):
        unescaped = GLib.Uri.unescape_string(tag, None) or tag
        if unescaped not in seen:
            seen.add(unescaped)
            tags.append(unescaped)
    return tags


def _session() -> Soup.Session:
    session = Soup.Session.new()
    session.set_user_agent(USER_AGENT)
    session.set_timeout(_CONNECT_TIMEOUT_SECONDS)
    session.set_idle_timeout(_IDLE_TIMEOUT_SECONDS)
    return session


def _deadline(cancellable: Gio.Cancellable, seconds: int) -> int:
    def fire() -> bool:
        cancellable.cancel()
        return False

    return GLib.timeout_add_seconds(seconds, fire)


class Client:
    """Finds and fetches releases. One operation at a time per instance."""

    def __init__(self, use_gh: bool = True) -> None:
        self._session = _session()
        self._cancellable: Gio.Cancellable | None = None
        self._deadline_id: int = 0
        self._gh = gh_path() if use_gh else None
        self._subprocess: Gio.Subprocess | None = None

    @property
    def channel(self) -> str:
        """Returns which channel lookups will try first: "gh" or "atom"."""

        return "gh" if self._gh else "atom"

    def cancel(self) -> None:
        """Abandons whatever is in flight. Safe to call when nothing is."""

        self._clear_deadline()
        if self._cancellable is not None:
            self._cancellable.cancel()
            self._cancellable = None
        if self._subprocess is not None:
            self._subprocess.force_exit()
            self._subprocess = None
        # Dropping a session with connections still open logs a runtime
        # warning from libsoup; abort closes them first.
        self._session.abort()

    def _clear_deadline(self) -> None:
        if self._deadline_id:
            GLib.source_remove(self._deadline_id)
            self._deadline_id = 0

    # --- Plain text over HTTPS -------------------------------------------

    def _get_text(
        self,
        url: str,
        on_done: Callable[[str | None, str | None], None],
    ) -> None:
        """GETs `url` and hands the body, or an error string, to on_done."""

        message = Soup.Message.new("GET", url)
        if message is None:
            on_done(None, f"could not build a request for {url}")
            return

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

            status = int(message.get_status())
            if status == 404:
                on_done(None, "not found")
                return
            if _rate_limited(message):
                on_done(None, rate_limit_message())
                return
            if status != 200:
                on_done(None, f"GitHub returned {status}")
                return

            data = body.get_data() if body is not None else None
            if not data:
                on_done(None, "GitHub returned an empty response")
                return
            on_done(data.decode("utf-8", errors="replace"), None)

        self._session.send_and_read_async(
            message, GLib.PRIORITY_DEFAULT, cancellable, finished
        )

    # --- The gh CLI ------------------------------------------------------

    def _run_gh(
        self,
        arguments: Sequence[str],
        on_done: Callable[[str | None, str | None], None],
    ) -> None:
        """Runs gh asynchronously and hands stdout, or an error, to on_done."""

        if self._gh is None:
            on_done(None, "gh is not installed")
            return

        try:
            process = Gio.Subprocess.new(
                [self._gh, *arguments],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error as error:
            on_done(None, _describe(error))
            return

        self._subprocess = process
        cancellable = Gio.Cancellable.new()
        self._cancellable = cancellable
        self._deadline_id = _deadline(cancellable, _GH_DEADLINE_SECONDS)

        def finished(source: Gio.Subprocess, result: Gio.AsyncResult) -> None:
            self._clear_deadline()
            self._subprocess = None
            try:
                _ok, stdout, stderr = source.communicate_utf8_finish(result)
            except GLib.Error as error:
                on_done(None, _describe(error))
                return

            if source.get_exit_status() != 0:
                detail = (stderr or "").strip().splitlines()
                on_done(None, detail[0] if detail else "gh failed")
                return
            on_done((stdout or "").strip(), None)

        process.communicate_utf8_async(None, cancellable, finished)

    def _gh_latest(
        self,
        repo: str,
        on_done: Callable[[str | None, str | None], None],
    ) -> None:
        """Asks gh for the latest release, falling back to the tag list."""

        def releases_done(output: str | None, error: str | None) -> None:
            if error is None and output:
                on_done(output.splitlines()[0].strip(), None)
                return
            # No releases published: ask for tags instead. gh reports this as
            # a failure with "Not Found" on stderr.
            def tags_done(tag_output: str | None, tag_error: str | None) -> None:
                if tag_error is not None or not tag_output:
                    on_done(None, tag_error or "gh returned nothing")
                    return
                chosen = best_tag([line.strip() for line in tag_output.splitlines()])
                on_done(chosen, None if chosen else "no version tags")

            self._run_gh(
                ["api", f"repos/{repo}/tags?per_page=100", "--jq", ".[].name"],
                tags_done,
            )

        self._run_gh(
            ["api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"],
            releases_done,
        )

    # --- The Atom feeds --------------------------------------------------

    def _atom_latest(
        self,
        repo: str,
        on_done: Callable[[str | None, str | None], None],
    ) -> None:
        """Reads the releases feed, falling back to the tags feed.

        Neither feed is an API endpoint, so neither spends any allowance. The
        difference from gh is that a feed lists pre-releases alongside
        releases, which is why the highest version is taken rather than the
        first entry.
        """

        def releases_done(xml: str | None, error: str | None) -> None:
            if error is not None and error != "not found":
                on_done(None, error)
                return
            tags = parse_feed_tags(xml or "")
            chosen = best_tag(tags)
            if chosen is not None:
                on_done(chosen, None)
                return

            def tags_done(tag_xml: str | None, tag_error: str | None) -> None:
                if tag_error is not None and tag_error != "not found":
                    on_done(None, tag_error)
                    return
                found = best_tag(parse_feed_tags(tag_xml or ""))
                if found is None:
                    on_done(None, "the repository has no releases or version tags")
                    return
                on_done(found, None)

            self._get_text(f"{GITHUB}/{repo}/tags.atom", tags_done)

        self._get_text(f"{GITHUB}/{repo}/releases.atom", releases_done)

    # --- The one lookup callers use --------------------------------------

    def latest_version(
        self,
        repo: str,
        on_done: Callable[[str | None, str | None], None],
    ) -> None:
        """Finds the newest release tag for `repo`.

        gh first when it is installed, then the Atom feeds. A gh failure is
        not reported as the answer: gh may be absent, signed out, or pointed
        at an enterprise host, and the feeds work in all three cases.
        """

        remembered = cached_tag(repo)
        if remembered is not None:
            # Answer through the main loop rather than straight away, so every
            # caller sees the same asynchronous shape either way.
            GLib.idle_add(lambda: (on_done(remembered, None), False)[1])
            return

        def finish(tag: str | None, error: str | None) -> None:
            if tag:
                _tag_cache[repo] = (tag, time.time())
            on_done(tag, error)

        if self._gh is None:
            self._atom_latest(repo, finish)
            return

        def gh_done(tag: str | None, _error: str | None) -> None:
            if tag:
                finish(tag, None)
                return
            self._atom_latest(repo, finish)

        self._gh_latest(repo, gh_done)

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
        """Streams a tag's source archive to `destination`.

        Fetched from github.com/<repo>/archive/refs/tags/<tag>.tar.gz rather
        than through the API, so it spends no API allowance: measured
        returning 200 while the API was answering 403 for exhaustion.

        **A Content-Length may or may not arrive.** codeload generates the
        archive on the first request for a tag and closes the connection to
        signal the end (``Soup.Encoding.EOF``, no length); once it has that
        archive cached it serves a real length. A new release is therefore
        exactly the case with no size, so the unknown path is the normal one.
        Without a length the fraction is bytes over `expected_bytes` -- what
        this repository's archive weighed last time -- or
        DEFAULT_ESTIMATE_BYTES, and an estimated fraction is held below 1.0
        until the stream actually ends, so the bar never claims to have
        finished before it has.
        """

        escaped = GLib.Uri.escape_string(tag, None, True)
        url = f"{GITHUB}/{repo}/archive/refs/tags/{escaped}.tar.gz"
        message = Soup.Message.new("GET", url)
        if message is None:
            on_done(False, "could not build a download request", 0)
            return

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

            if _rate_limited(message):
                fail(rate_limit_message())
                return
            if int(message.get_status()) != 200:
                fail(f"GitHub returned {int(message.get_status())} for the download")
                return

            length = message.get_response_headers().get_content_length()
            if length and length > 0:
                state["total"], state["measured"] = length, True
            else:
                state["total"] = expected_bytes or DEFAULT_ESTIMATE_BYTES
            if state["measured"] and state["total"] > MAX_DOWNLOAD_BYTES:
                fail("the download is larger than this add-on is willing to fetch")
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
                    fail("the download exceeded the size this add-on will fetch")
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
