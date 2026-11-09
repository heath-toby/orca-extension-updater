#!/usr/bin/env bash
# Extension Updater for Orca -- Uninstaller
#
# Removes the extension package and revokes its Orca approval. Settings are
# left alone, under /org/gnome/orca/<profile>/extensions/extension-updater/,
# so a reinstall picks up where you left off. Pass --purge to remove them and
# the data directory too.

set -euo pipefail

ADDON_NAME="extension_updater"
# Orca sanitises the namespace for the settings path, and an underscore becomes
# a dash -- so the package is extension_updater and its dconf path is not.
# Getting this wrong is not harmless: a copy of this script once carried the
# name of the project it was adapted from, and --purge wiped a different
# add-on's settings.
SETTINGS_NAME="extension-updater"
ORCA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/orca"
ADDON_DIR="$ORCA_DIR/extensions/$ADDON_NAME"
DATA_DIR="$ORCA_DIR/extension-updater"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

info() { echo "  [+] $*"; }

echo ""
echo "=== Extension Updater for Orca -- Uninstaller ==="
echo ""

# Revoke first: once the directory is gone Orca can no longer identify it.
if orca --revoke-extension "$ADDON_NAME" >/dev/null 2>&1; then
    info "Revoked Orca's approval of '$ADDON_NAME'."
else
    info "No Orca approval to revoke (already removed)."
fi

if [ -d "$ADDON_DIR" ]; then
    rm -rf "$ADDON_DIR"
    info "Removed $ADDON_DIR"
else
    info "No installed extension at $ADDON_DIR (already removed)."
fi

if [ "$PURGE" -eq 1 ]; then
    dconf reset -f "/org/gnome/orca/default/extensions/$SETTINGS_NAME/" 2>/dev/null \
        && info "Removed Extension Updater's settings." \
        || info "No settings to remove."
    if [ -d "$DATA_DIR" ]; then
        rm -rf "$DATA_DIR"
        info "Removed $DATA_DIR (log and rollback backups)."
    fi
else
    info "Settings and data kept. Re-run with --purge to remove them too."
    info "Note: $DATA_DIR holds the rollback backups of other add-ons."
fi

echo ""
echo "=== Uninstall complete ==="
echo ""
echo "  Restart Orca for the change to take effect:"
echo "    orca --replace &"
echo ""
