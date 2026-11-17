#!/usr/bin/env bash
# Build the macOS installer for the Kaitoi plugin for DaVinci Resolve.
#
#   packaging/macos/build-pkg.sh
#
# The package itself installs one folder, /Library/Application Support/Kaitoi/Resolve:
# the plugin's code (outside Resolve's Scripts folders, where Resolve would
# list each of its files in the Scripts menu), "Uninstall Kaitoi.command",
# which removes all of it again, and, under install/, the three files Resolve
# has to find:
#   - the "Kaitoi" script, listed under Workspace > Scripts > Edit
#   - the Kaitoi generator and effect, for Resolve's Effects Library
# Those three are copied when the install ends into the Resolve folder of the
# person installing, inside their own Library, by that person and not by the
# system. Resolve's folder for every user of the Mac is never written: every
# account can write there. The package must not carry
# Resolve's folders itself: an installer sets the owner and permissions of
# every folder it carries, and Resolve keeps those folders open to its user,
# to Resolve itself and to other tools. Carried, they would be closed.
#
# Signing and notarization follow the Kaitoi CLI installer and are switched on
# by the same variables:
#   APPLE_DEVELOPER_ID_INSTALLER   "Developer ID Installer: Name (TEAMID)": sign the package
#   APPLE_NOTARY_PROFILE           a notarytool keychain profile: notarize and staple it
#   MACOS_REQUIRE_SIGNING=1        fail instead of building an unsigned package
#   MACOS_REQUIRE_NOTARIZATION=1   fail instead of building one that is not notarized
# The plugin is Python and pictures, with no program of its own to sign, so
# only the package itself carries a signature.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT_DIR="${OUT_DIR:-$REPO/dist}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO/kaitoi_resolve/__init__.py")"
[ -n "$VERSION" ] || { echo "ERROR: no __version__ in kaitoi_resolve/__init__.py"; exit 1; }

APPLE_DEVELOPER_ID_INSTALLER="${APPLE_DEVELOPER_ID_INSTALLER:-}"
APPLE_NOTARY_PROFILE="${APPLE_NOTARY_PROFILE:-}"
if [ -z "$APPLE_DEVELOPER_ID_INSTALLER" ] && [ "${MACOS_REQUIRE_SIGNING:-0}" = "1" ]; then
    echo "ERROR: MACOS_REQUIRE_SIGNING=1 but APPLE_DEVELOPER_ID_INSTALLER is not set."; exit 1
fi
if [ -z "$APPLE_NOTARY_PROFILE" ] && [ "${MACOS_REQUIRE_NOTARIZATION:-0}" = "1" ]; then
    echo "ERROR: MACOS_REQUIRE_NOTARIZATION=1 but APPLE_NOTARY_PROFILE is not set."; exit 1
fi
if [ -n "$APPLE_NOTARY_PROFILE" ] && [ -z "$APPLE_DEVELOPER_ID_INSTALLER" ]; then
    echo "ERROR: APPLE_NOTARY_PROFILE needs APPLE_DEVELOPER_ID_INSTALLER: only a signed package can be notarized."; exit 1
fi

echo "=== Building the Kaitoi for DaVinci Resolve installer v$VERSION ==="
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/root"  # what lands in /Library/Application Support/Kaitoi/Resolve
STAGED="$ROOT/install"
mkdir -p "$STAGED/Generators" "$STAGED/Effects"

cp "$REPO/Kaitoi.py" "$STAGED/Kaitoi.py"
cp "$REPO/templates/Edit/Generators/Kaitoi.setting" "$STAGED/Generators/Kaitoi.setting"
cp "$REPO/templates/Edit/Effects/Kaitoi.setting" "$STAGED/Effects/Kaitoi.setting"
cat > "$STAGED/README.txt" <<'README'
Kaitoi for DaVinci Resolve

The installer copies these files into DaVinci Resolve's folder for the person
who runs it. Each person on this Mac who wants Kaitoi in DaVinci Resolve runs
the installer once.

If it could not copy them, the installer's log says why: in the installer,
choose Window > Installer Log, then Show All Logs, and search for "Kaitoi:".
Put that right and run the installer again, or copy the files yourself into
your own
  ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion

Do not copy them there if the log says that one of those folders is a link,
or that your home or Library folder is open to other accounts of this Mac.
DaVinci Resolve runs the Kaitoi script as you, and in a folder other people
can write to, any of them can change it.

  Kaitoi.py                  ->  Scripts/Edit/Kaitoi.py
  Generators/Kaitoi.setting  ->  Templates/Edit/Generators/Kaitoi.setting
  Effects/Kaitoi.setting     ->  Templates/Edit/Effects/Kaitoi.setting

Then restart DaVinci Resolve and open Workspace > Scripts > Edit > Kaitoi.

To remove Kaitoi, press Uninstall Kaitoi on the Account tab of the Kaitoi
window, or double-click "Uninstall Kaitoi.command" in
/Library/Application Support/Kaitoi/Resolve. Both do the same, and macOS asks
for your password. Your results and your Kaitoi sign-in are not touched.

To remove it by hand instead, delete from that Fusion folder:
  the three files above
  Templates/Edit/Generators/Kaitoi and Templates/Edit/Effects/Kaitoi, the
    two folders of effects Kaitoi made for you
and, to forget your Kaitoi settings as well, the folder ~/.kaitoi_resolve.
Then delete the folder /Library/Application Support/Kaitoi/Resolve. Every
person on this Mac shares it, so Kaitoi stops working in DaVinci Resolve for
all of them. Then run in Terminal:
  sudo pkgutil --forget io.kaitoi.resolve-plugin
README
# The package as it is in the repository, without caches or editor leftovers.
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' "$REPO/kaitoi_resolve" "$ROOT/"
cp "$REPO/LICENSE" "$ROOT/LICENSE"
# Beside the code, where only an administrator can change it, and under the
# name a double-click opens in Terminal.
cp "$HERE/uninstall" "$ROOT/Uninstall Kaitoi.command"
chmod 755 "$ROOT/Uninstall Kaitoi.command"
find "$ROOT" -name '.DS_Store' -delete
# Files carry extended attributes from wherever they were checked out; left
# on, each would be installed with a "._" twin beside it.
xattr -cr "$ROOT"
chmod -R go-w "$ROOT"

SCRIPTS="$WORK/scripts"
mkdir -p "$SCRIPTS"
cp "$HERE/postinstall" "$SCRIPTS/postinstall"
chmod 755 "$SCRIPTS/postinstall"

mkdir -p "$OUT_DIR"
COMPONENT="$WORK/component.pkg"
PKG="$OUT_DIR/Kaitoi-for-DaVinci-Resolve-$VERSION.pkg"
# Installed at its own folder, so the package lists that folder and nothing
# above it: not /Library, and none of Resolve's.
COPYFILE_DISABLE=1 pkgbuild --root "$ROOT" --install-location "/Library/Application Support/Kaitoi/Resolve" --identifier "io.kaitoi.resolve-plugin" --version "$VERSION" \
    --ownership recommended --scripts "$SCRIPTS" "$COMPONENT"

# The whole point of the layout above, checked before anything is signed or
# sent to Apple: fail the build if a later change puts someone else's folder
# back into the package.
"$HERE/check-payload.sh" "$COMPONENT"

# For this Mac's own disk only. Offered another disk, the code would land
# there while the last step and Resolve both look on this one.
DISTRIBUTION="$WORK/distribution.xml"
productbuild --synthesize --package "$COMPONENT" "$DISTRIBUTION"
sed -i '' 's|</installer-gui-script>|    <domains enable_localSystem="true" enable_anywhere="false" enable_currentUserHome="false"/>\
</installer-gui-script>|' "$DISTRIBUTION"
grep -q '<domains enable_localSystem="true" enable_anywhere="false" enable_currentUserHome="false"/>' "$DISTRIBUTION" \
    || { echo "ERROR: could not limit the package to this Mac's own disk."; exit 1; }

if [ -n "$APPLE_DEVELOPER_ID_INSTALLER" ]; then
    productbuild --sign "$APPLE_DEVELOPER_ID_INSTALLER" --timestamp --distribution "$DISTRIBUTION" --package-path "$WORK" "$PKG"
    pkgutil --check-signature "$PKG"
else
    productbuild --distribution "$DISTRIBUTION" --package-path "$WORK" "$PKG"
    echo "NOTE: the package is not signed. macOS will warn before it opens."
fi

if [ -n "$APPLE_NOTARY_PROFILE" ]; then
    echo "Notarizing (this waits for Apple)..."
    xcrun notarytool submit "$PKG" --keychain-profile "$APPLE_NOTARY_PROFILE" --wait
    xcrun stapler staple "$PKG"
    xcrun stapler validate "$PKG"
fi

(cd "$OUT_DIR" && shasum -a 256 "$(basename "$PKG")" > "$(basename "$PKG").sha256")
echo "Built $PKG"
