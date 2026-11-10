# Extension Updater for Orca

Tells you when your Orca add-ons have new releases, and installs the ones you
tick.

An Orca 51 user extension. It reads the metadata Orca already holds for every
installed extension, asks GitHub whether a newer release exists, and offers
the updates in a dialog with a tickbox each. Nothing is ever updated without
being ticked.

## Install

```bash
./install.sh
orca --replace &
```

Needs Orca 51 or later, libsoup 3 and GTK 3, all of which the installer
checks for before it does anything.

## Using it

- **Orca+Shift+U** checks now. It always answers: the dialog opens if there
  are updates, and it says "no extension updates available" if there are not,
  so the key never leaves you wondering whether the check happened.
- A check also runs shortly after Orca starts, and then on the interval you
  choose.
- Settings are in Orca Preferences, under User Extensions, Extension Updater:
  how often to check, whether to check at startup, and whether to log.

When updates are found you get a tickbox per add-on reading, for example,
"Polyglot 2.10.0 to 2.11.0". Everything starts ticked. **Update selected**
takes what you have ticked; **Update all** takes everything regardless;
**Not now** does nothing.

While updating you get a progress bar per add-on, driven from 0 to 100 and
then reset for the next one, so you hear one sweep per add-on rather than one
long one. The bar reports which add-on it belongs to, so tabbing to it answers
"which one is this".

## What it can and cannot see

An extension can be checked only if it declares where it came from, using
Orca's own `WEBSITE` metadata field pointing at a GitHub project:

```python
class MyExtension(Extension):
    GROUP_LABEL = "My Extension"
    VERSION = "1.0.0"
    WEBSITE = "https://github.com/someone/my-extension"
```

Orca reads that with `ast`, without importing the extension, and shows it as a
link in Preferences → User Extensions → Info. Extensions that declare no
website, or whose version is not a plain `1.2.3`, are listed as unmanaged and
left alone.

Most extensions in the wild declare no website, so you can name one yourself:
in this add-on's settings, **Repositories for add-ons that do not declare one**
maps an installed extension to its repository. Use the installed name — a
folder such as `polyglot`, or a file such as `resource_monitor.py` — and
`owner/repository` or the project's address. A name you give wins over a
declared one, so it is also how to correct metadata that points somewhere
stale. The repository still has to publish releases or version tags.

Two optional module-level declarations control what an update installs. Both
are read with `ast`, never executed, and any entry that is not a plain file or
directory name beside the source is discarded:

- `__update_exclude__ = ("themes",)` — paths inside the package that must not
  be installed. Audio Themes needs this: its themes live outside the installed
  package deliberately, because Orca's approval hash covers every file in the
  package, so a theme edited in place would silently un-approve the extension.
- `__update_include__ = ("sounds",)` — paths at the repository root that
  belong inside the installed package. Clock needs this: its chimes live at the
  repository root and its `install.sh` copies them in.

## The progress bar's beeps come from Orca

This add-on generates no sound of its own. Orca already presents other
applications' progress bars, so a real `Gtk.ProgressBar` is heard the way
every other progress bar on the system is heard.

That means the beeping is governed by Orca's own setting, in Orca Preferences
→ General → "Beep when a progress bar updates". With it off, the bar is still
readable by tabbing to it, just silent.

## How an update is made safe

Orca approves an extension by a hash over every file in its package, so
installing an update means approving code that was just downloaded. Three
things bound that:

1. The repository comes from the **installed** extension's metadata, never
   from anything inside the download.
2. Every Python file must compile before anything is swapped in.
3. The previous version is kept, and restored if any later step fails.

The order is: download to a fresh temporary directory, extract with
`tarfile`'s `data` filter (which refuses absolute paths, parent traversal and
device nodes), compile-check every `.py`, build the exact tree to be installed
in a staging directory, move the installed version aside, move the new one in,
approve it, and then **reload it** to prove it actually loads.

That last step is the load-bearing one. Orca catches a failed import, a
raising constructor and a raising `on_ready`. It does **not** catch a raising
`_get_commands()`, which propagates out of `load_user_settings` during
startup — so a bad release that raises while building its keybindings would
not fail at update time, it would fail at the *next* Orca start, as a screen
reader that will not come up and with no add-on running to undo it. Reloading
the extension here, inside a `try`, provokes that failure while Orca is still
up and a rollback is still possible.

If a rollback itself fails to come back up, the add-on stops, says so, and
tells you to restart Orca. That is the one state it cannot repair from inside
Orca.

**Unless it was already broken.** Whether Orca has the extension loaded is
recorded before anything is touched, because "it does not load" is only
evidence against an update if it loaded a moment ago. An add-on that was not
loading beforehand is updated, kept, and reported as installed-but-not-loaded,
with the update explicitly not blamed. This matters because a failed extension
import is **silent**: Orca logs it at WARNING and starts anyway, so an add-on
can be absent for weeks with no symptom but the thing it used to do no longer
happening.

`tools/probe-extensions.py` reports which installed extensions can actually be
imported, which is the quickest way to find that state.

### What it does not do

It installs code, not dependencies. The downloaded `install.sh` is never run:
it is freshly downloaded shell, and it also does work an unattended update
should not — building a virtualenv, fetching word lists, migrating themes,
resetting settings. The copy rule reproduces what each add-on's own
`install.sh` produces for the installed package, and `tests-installer.py`
asserts that by diffing the two trees for every add-on on this machine. But if
a release gains a new dependency, run that add-on's own `install.sh` from a
clone; the completion dialog says which add-ons ship one.

## Restarting

Updates are live as soon as they are installed, because each one is reloaded
and checked.

The exception is this add-on updating itself. Reloading it would purge its own
module from `sys.modules` and re-import it while the code doing the reloading
is still running inside the old one, so it is installed and approved but not
reloaded, and the completion dialog says a restart is needed to run it. It
does not claim to be active when it is not.

The restart is chosen, not assumed. Under systemd, `orca.service` carries
`Restart=always`, so `systemctl --user restart orca` is used and
`orca --replace` would fight the service manager. Started as an ordinary
application, `orca --replace` is used — detached into its own session, because
`--replace` SIGKILLs every Orca it finds and would otherwise kill the process
carrying out the restart.

## Files it writes

Everything lives in `~/.local/share/orca/extension-updater/`, outside the
extension package — Orca hashes every file inside a package, so a log written
in there would un-approve the add-on on first write.

- `debug.log` — only when logging is switched on. Dated stamps, rolled at 2 MB.
- `backups/<extension>/` — the previous version of each updated add-on, kept
  for rollback.
- `staging/` — scratch space during an update, removed afterwards.

## Where it looks, and why not the API

GitHub's API allows 60 requests an hour without a sign-in. A check costs one
request per add-on and each update costs one more, so with ten add-ons a check
and a full round of updates is nineteen of those sixty — and the add-on that
runs out is always whichever sorts last. **So this add-on does not use the API
at all.** Two routes are used instead, both measured answering 200 while the
unauthenticated API was returning 403 for exhaustion:

1. **The `gh` CLI**, when it is installed and signed in. It uses your own
   token, which is allowed 5,000 requests an hour, and it is the only route
   that honours GitHub's own idea of the *latest* release — so a pre-release
   is never offered as an update. A check of nine repositories takes about 4.4
   seconds and one request of the five thousand.
2. **The releases and tags Atom feeds** on github.com, used when `gh` is
   absent, signed out, or fails. These are not API endpoints: no
   `x-ratelimit` headers, no allowance spent at all. The same check takes
   about 2.2 seconds. A feed lists pre-releases beside releases, so the
   highest version-like tag is taken rather than the first entry, and anything
   that is not a plain `1.2.3` is skipped rather than guessed at.

Downloads use `github.com/<repo>/archive/refs/tags/<tag>.tar.gz`, which is
codeload rather than the API, so an update spends no allowance either.

The setting *Use the GitHub CLI for checks when it is installed* chooses
between the two. It is on by default: not offering a pre-release to a screen
reader user is worth four seconds.

Release tags are remembered for fifteen minutes regardless, so pressing the
key twice does not repeat the work. That window is shorter than the shortest
check interval, so a scheduled check never answers from the cache.

github.com can still refuse a burst of requests with a 403 or 429. That is
reported as "try again shortly" rather than as a fault, and a refused download
stops the run instead of attempting the rest.

### Why NVDA never shows you this

NVDA's add-on store fetches **one** aggregated index from NV Access, so it
makes a single request however many add-ons you have and GitHub's per-repo
limits never arise. There is no such index for extensions like these, so a
per-repository check is the only option — which makes the choice of channel
the thing that matters.

## Tests

Run after `./install.sh`, against the installed copy:

```bash
./tests-inventory.py     # which websites and versions resolve
./tests-installer.py     # the copy rule, extraction safety, rollback
./tests-schedule.py      # interval maths, with GLib's timers recorded
./tests-dialogs.py       # dialog structure, without showing any window
./tests-end-to-end.py    # a real download and install (uses the network)
```

`tests-dialogs.py` never shows a window, because these tests run on a live
desktop and a dialog that appeared would take the keyboard focus away from
whatever was being read.

Two things the tests cannot answer, both field tests:

1. How the dialogs sound and navigate — tab through the tickboxes with Orca
   reading and confirm each announces its name and state.
2. Whether Orca's progress-bar beeps and its speech talk over each other
   during a real multi-add-on update.
