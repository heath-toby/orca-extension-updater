#!/usr/bin/env bash
# Extension Updater for Orca -- Installer
#
# Installs Extension Updater as an Orca 51 user extension, into
# ~/.local/share/orca/extensions/, and approves it so Orca will load it.
#
# Re-running is safe. Because approval is by content hash, the script
# re-approves on every run -- which is what you want after editing.

set -euo pipefail

ADDON_NAME="extension_updater"
ORCA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/orca"
EXTENSIONS_DIR="$ORCA_DIR/extensions"
ADDON_DIR="$EXTENSIONS_DIR/$ADDON_NAME"
DATA_DIR="$ORCA_DIR/extension-updater"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/$ADDON_NAME"

info()  { echo "  [+] $*"; }
warn()  { echo "  [!] $*"; }
error() { echo "  [ERROR] $*" >&2; exit 1; }

echo ""
echo "=== Extension Updater for Orca -- Installer ==="
echo ""

# Probe via extension_loader, not orca.extension: importing orca.extension
# first hits a circular import inside Orca itself (live_region_presenter
# imports it mid-initialisation). extension_loader pulls in command_manager
# ahead of it, so this import order is the one that works.
if ! python3 -c "import orca.extension_loader" 2>/dev/null; then
    error "Orca 51 or later with extension support not found."
fi
info "Orca with extension support found."

# libsoup 3 is how the release checks and downloads are made without blocking
# Orca's main loop. There is no fallback: a blocking HTTP call inside the
# screen reader is the bug this add-on must not have.
if ! python3 -c "
import gi
gi.require_version('Soup', '3.0')
from gi.repository import Soup
assert Soup.Session.new() is not None
" 2>/dev/null; then
    error "libsoup 3 Python bindings not found. On most distributions that is libsoup-3 plus gobject-introspection."
fi
info "libsoup 3 available."

if ! python3 -c "
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Atk', '1.0')
from gi.repository import Atk, Gtk
" 2>/dev/null; then
    error "GTK 3 Python bindings not found; the update dialogs need them."
fi
info "GTK 3 available."

[ -d "$SOURCE_DIR" ] || error "Source directory '$SOURCE_DIR' not found."

rm -rf "$SOURCE_DIR/__pycache__"

mkdir -p "$ADDON_DIR"
# Remove files that no longer exist in the source, so the installed package --
# and therefore its approval hash -- matches the source.
find "$ADDON_DIR" -maxdepth 1 -name '*.py' -delete
rm -rf "$ADDON_DIR/__pycache__"
cp "$SOURCE_DIR"/*.py "$ADDON_DIR/"
info "Installed extension package to $ADDON_DIR"

# Backups, staging and the log live here, outside the package: Orca hashes
# every file inside an extension's directory, so anything written in there
# would un-approve the add-on.
mkdir -p "$DATA_DIR"
info "Data directory ready at $DATA_DIR"

if orca --approve-extension "$ADDON_NAME" >/dev/null 2>&1; then
    info "Approved '$ADDON_NAME' with Orca."
else
    error "Could not approve the extension. Run: orca --approve-extension $ADDON_NAME"
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "  Restart Orca to activate:"
echo "    orca --replace &"
echo ""
echo "  Check for updates now:  Orca+Shift+U"
echo "  Settings: Orca Preferences -> User Extensions -> Extension Updater"
echo ""
echo "  It can only check an extension that declares a WEBSITE pointing at a"
echo "  GitHub project. Orca reads that from the extension's metadata without"
echo "  importing it; add-ons that declare none are listed as unmanaged."
echo ""
echo "  The progress bar's beeps come from Orca's own setting, under"
echo "  Preferences -> General -> 'Beep when a progress bar updates'."
echo ""
