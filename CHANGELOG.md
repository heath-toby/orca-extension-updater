# Changelog

## 1.4.1

**"One version could not be checked" now says which, and stops saying it.**

Heard on every check: *"No extension updates available, one version could not
be checked."* It was true and useless. The add-on was Signal Filter, whose
repository has never published a release or a tag, so there is nothing there
to compare a version against — a settled fact about that repository rather
than a failure of the check, and counting it reported a problem without saying
where.

- A repository that publishes nothing is now **named**, and described as
  having nothing to check rather than as having failed: *"No extension updates
  available. Signal Filter publishes no releases, so there is nothing to
  check."*
- It is said **once per Orca session**. Permanent facts do not need repeating
  on every keypress, and the log records it every time regardless.
- A check that genuinely failed is reported separately, with the add-on's name
  and the reason, because that one may be worth retrying.
- A refusal for request volume still says the check could not be made, rather
  than claiming there are no updates — the distinction being that we did not
  find out.

## 1.4.0

**It no longer uses GitHub's rationed API at all.**

Sixty requests an hour cannot support ten add-ons, and the add-on that lost
was always whichever sorted last. Rather than ration more carefully, the
checks now go somewhere that is not rationed. Both routes were measured with
the unauthenticated API returning 403 for exhaustion; both answered 200.

1. **The `gh` CLI**, when it is installed and signed in. It uses your own
   token, which is allowed 5,000 requests an hour rather than 60, and it is
   the only route that honours GitHub's own idea of the *latest* release, so a
   pre-release is never offered as an update. A full check of nine
   repositories took 4.4 seconds and one request of the five thousand.
2. **The releases and tags Atom feeds** on github.com, used when `gh` is
   absent, signed out, or fails for any reason. These are not API endpoints:
   they carry no `x-ratelimit` headers and spend no allowance whatsoever. The
   same check took 2.2 seconds. Because a feed lists pre-releases beside
   releases, the highest version-like tag is taken rather than the first
   entry, and anything that is not a plain `1.2.3` is skipped rather than
   guessed at.
3. **The REST API is gone**, for checks and for downloads alike. Archives now
   come from `github.com/<repo>/archive/refs/tags/<tag>.tar.gz`, which is
   codeload rather than the API.

A new setting, *Use the GitHub CLI for checks when it is installed*, chooses
between the two. It is on by default, because not offering a pre-release to a
screen reader user is worth four seconds.

The rate-limit machinery from 1.0.2 and 1.2.0 is reduced to what is still
needed: github.com can still refuse a burst with 403 or 429, and that is
reported as "try again shortly" rather than as a fault.

### On NVDA

Fair point, and the difference is architectural rather than a matter of care.
NVDA's add-on store fetches **one** aggregated index from NV Access, so it
makes a single request however many add-ons you have, and GitHub's per-repo
limits never enter into it. There is no such index for these add-ons, so a
per-repository check is the only option — which makes the choice of channel
the thing that matters, and that is what this release fixes.

## 1.3.0

**An add-on that was already failing to load no longer looks like a failed
update.**

Reported from the field as "the new version did not load and the old version
could not be put back", which sounded catastrophic and was not. Both halves
were true, and neither was this add-on's fault: Wayland Clipboard had stopped
loading some time earlier, because Orca renamed `orca.clipboard` to
`orca.clipboard_manager` and the add-on imported the old name. Orca catches a
failed extension import, logs it at WARNING, and starts anyway, so nothing
ever said so. The update installed correctly and the rollback restored
correctly; only the "does it load?" test failed, twice, for a reason that had
nothing to do with either.

- Whether Orca has the extension loaded is now recorded **before** anything is
  touched. "It does not load" is only evidence against an update if it loaded a
  moment ago.
- An add-on that was not loading beforehand is updated, kept, and reported
  plainly: it installed, Orca does not load it, and the update is not the
  cause. An add-on that *was* loading and then stops is still rolled back,
  exactly as before.
- New: `tools/probe-extensions.py` reports which installed extensions can
  actually be imported. This is the diagnostic that was missing — the Wayland
  Clipboard fault would have taken one second to find.

## 1.2.0

**Pressing the key again no longer spends a request per add-on before it
downloads anything.**

Found in the field, and the diagnosis is worth recording because the symptom
pointed elsewhere. A run of eight updates installed seven and left Wayland
Clipboard behind; it looked add-on specific, and it was not. The allowance is
spent one request per add-on to check, plus one more per add-on to install, so
with ten add-ons a check and a full round of updates is nineteen of the sixty
requests an hour allows. Retrying spends another nineteen. The add-on that
loses is always the last one in the run, which is alphabetical -- hence always
Wayland Clipboard.

- Release tags are now remembered for fifteen minutes, so a second attempt
  spends its requests on downloads rather than on asking again. Fifteen
  minutes is measured against the shortest check interval, an hour, so a
  scheduled check never answers from the cache.
- A download refused for the allowance now **stops the run** instead of
  attempting the rest, which would each fail the same way. The ones not
  reached are named, so it is clear there is something to come back for.

## 1.1.0

**You can now name the repository for an add-on that does not declare one.**

Only this family of add-ons declares Orca's `WEBSITE` field, so until now a
third-party extension could never be followed, however willing its author. A
new dictionary setting, *Repositories for add-ons that do not declare one*,
maps an installed extension to `owner/repository`. A name you give wins over
a declared one, which is also the way to correct metadata pointing somewhere
stale.

It accepts what people actually type: `owner/repo`, a full project address,
`github.com/owner/repo`, a trailing slash, a `.git` suffix, and any
capitalisation of the host. It rejects anything that is not exactly an owner
and a repository, because the result is interpolated into an API path --
`/etc/passwd` was being accepted as `etc/passwd` while that was being written,
and now is not.

The repository still has to publish releases or `vX.Y.Z` tags; naming it does
not conjure a version to compare against.

## 1.0.2

**An update stopped by GitHub's hourly limit now says so.**

Found the hard way: a run of eight updates installed seven and failed the
last one with "GitHub returned 403 for the download", which reads like a
fault to investigate rather than something that fixes itself in half an hour.
The allowance was simply spent -- a check costs one request per add-on and
each update costs one more, so with nine add-ons a check plus a round of
updates is a third of the sixty requests an hour allows.

- The download path now recognises a refusal for the rate limit, as the
  lookup path already did, and both now say when the limit resets.
- The allowance is tracked from the headers every response already carries,
  so a request that is certain to be refused is not made at all.
- A check that could not complete no longer reports "no extension updates
  available", which was claiming an answer it did not have. It now says it
  could not find out, and why.
- The completion dialog adds one line distinguishing a wait from a fault:
  press Orca+Shift+U again once the limit resets and the rest will install.

## 1.0.1

**Updating the updater no longer reloads the updater.**

Every other add-on is reloaded after it is installed, which is how a release
that will not load gets caught while a rollback is still possible. Doing that
to this extension would purge this very module from `sys.modules` and
re-import it while the code running the reload is still executing inside the
old one -- and Orca's reload tears down and re-registers every extension's
commands on the way through. The new version is installed and approved
instead, and the completion dialog says plainly that a restart is needed to
run it, rather than calling it active when it is not.

Also fixes a test that compared staged bytes against the *installed* copy of
an add-on rather than against its source, so it failed whenever a version had
been bumped without reinstalling -- which said nothing about whether the copy
was faithful, the thing it was meant to check.

## 1.0.0

First release.

- Reads every installed extension's metadata through Orca's own
  `extension_loader.discover_user_extensions`, which parses it with `ast` and
  never imports the extension. The repository comes from Orca's existing
  `WEBSITE` field, so nothing new had to be invented to say where an add-on
  came from.
- Checks GitHub for a newer release per add-on, falling back to version tags
  where a repository publishes no releases, and never downgrades — a
  rolled-back tag is skipped rather than installed.
- All network work goes through libsoup 3 asynchronously on the GLib main
  loop. Nothing blocks Orca: a blocking HTTP call inside the screen reader
  would stall speech, and `orca.service` ships `WatchdogSec=10`, so it could
  also have systemd kill Orca mid-update.
- An accessible GTK 3 dialog with a tickbox per update, Update selected and
  Update all.
- One `Gtk.ProgressBar` per add-on, driven 0 to 100 and reset between add-ons.
  The add-on ships no audio code at all; Orca already presents other
  applications' progress bars, so the beeps come from Orca's own
  `beepProgressBarUpdates` setting.
- Download fills the first half of the bar, extraction and the compile check
  the next quarter, and installation the last quarter.
- Progress during the download is measured against the previous download's
  size, remembered per repository. GitHub sends no `Content-Length` for a tag
  it has not served before — which is exactly the case for a new release — so
  an estimated fraction is held below full until the stream actually ends.
- Every update is reversible: the previous version is kept, restored on any
  failure, re-approved and reloaded. A rollback that cannot be brought back up
  is reported as needing an Orca restart rather than passed over.
- Each new version is reloaded inside a `try` before the update is called
  done. This is what catches a release whose `_get_commands()` raises, which
  Orca does not guard and which would otherwise break the next Orca start
  rather than this update.
- Archives are extracted with `tarfile`'s `data` filter, and the optional
  `__update_exclude__` and `__update_include__` lists are read with `ast` and
  sanitised, so a downloaded package cannot name a path outside its own tree.
- Orca+Shift+U checks on demand, and always says what it found. Clash-checked
  against Orca's own bindings, which use Orca+U and Orca+Ctrl+U for flat
  review, and against the other add-ons installed here.
- Configurable interval: hourly, six-hourly, twelve-hourly, daily, weekly,
  monthly or off, plus a check shortly after Orca starts. The last check is
  remembered on disk, so a weekly check does not slide forward every time
  Orca restarts.
