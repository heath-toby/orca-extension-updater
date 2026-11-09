# Changelog

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
