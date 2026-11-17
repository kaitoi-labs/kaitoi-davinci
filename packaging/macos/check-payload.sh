#!/usr/bin/env bash
# Refuse a Kaitoi package that touches anything but its own folder.
#
#   packaging/macos/check-payload.sh <package.pkg>
#
# An installer sets the owner and permissions of every folder a package
# carries. DaVinci Resolve keeps its folders open to its user, so a package
# that carried them would close them. The package must install into
# /Library/Application Support/Kaitoi/Resolve and list only what belongs
# there: its code, install/, the uninstaller and the licence.
#
# The listing is read once into a variable and searched there. Piped straight
# into "grep -q", the lister is cut off at the first match, and under
# "set -o pipefail" that reads as a failure whatever grep found.

set -euo pipefail

pkg="${1:?usage: check-payload.sh <package.pkg>}"
home="/Library/Application Support/Kaitoi/Resolve"

# Where it installs: a package that lists only "./install" and
# "./kaitoi_resolve" but installs them into someone else's folder is no better.
expanded="$(mktemp -d)"
trap 'rm -rf "$expanded"' EXIT
pkgutil --expand "$pkg" "$expanded/package"
locations="$(find "$expanded/package" -name PackageInfo -exec sed -n 's/.*install-location="\([^"]*\)".*/\1/p' {} + || true)"
if [ -z "$locations" ]; then
    echo "ERROR: could not read where $pkg installs."; exit 1
fi
elsewhere="$(grep -vxF "$home" <<<"$locations" || true)"
if [ -n "$elsewhere" ]; then
    echo "ERROR: the package installs into a folder that is not Kaitoi's own: $(head -1 <<<"$elsewhere")"; exit 1
fi

files="$(pkgutil --payload-files "$pkg")"
for wanted in "./install/Kaitoi.py" "./install/Generators/Kaitoi.setting" "./install/Effects/Kaitoi.setting" "./kaitoi_resolve/ui.py"; do
    if ! grep -qxF "$wanted" <<<"$files"; then
        echo "ERROR: the package does not hold $wanted."; exit 1
    fi
done
# Nothing at its top but what is ours ("._" entries only carry a file's attributes).
foreign="$(grep -vE '^\.$|^\./(install|kaitoi_resolve)(/|$)|^\./(LICENSE|Uninstall Kaitoi\.command)$|^\./\._(install|kaitoi_resolve|LICENSE|Uninstall Kaitoi\.command)$' <<<"$files" || true)"
if [ -n "$foreign" ]; then
    echo "ERROR: the package carries folders that are not Kaitoi's own:"
    head -5 <<<"$foreign"
    exit 1
fi
echo "Package installs into Kaitoi's own folder only ($(wc -l <<<"$files" | tr -d ' ') entries)."
