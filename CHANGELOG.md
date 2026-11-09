# Changelog

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
