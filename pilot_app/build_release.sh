#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$($ROOT_DIR/.venv-pilot/bin/python -c 'from pilot_app import __version__; print(__version__)')"
ARCHIVE="cityu-mail-pilot-$VERSION.tar.gz"

# LICENSE ships with the runtime package: the installer copies it next to the
# code it just installed, which is what AGPL expects when you hand somebody the
# software. A root README is included when present so a freshly unpacked release
# explains itself. (The first version of this script omitted both, and the
# installer died halfway through an upgrade on the missing file.)
EXTRA=()
[[ -f "$ROOT_DIR/LICENSE" ]] && EXTRA+=(LICENSE)
[[ -f "$ROOT_DIR/README.md" ]] && EXTRA+=(README.md)

mkdir -p "$ROOT_DIR/dist"
# macOS tar writes each file's extended attributes into the archive as an
# AppleDouble `._name` member. bsdtar hides those when listing, so the archive
# looked clean here and unpacked as 52 junk files plus a screen of warnings for
# anyone on Linux -- most of a third of the release was metadata. COPYFILE_DISABLE
# is the supported switch and is a no-op on GNU tar; the --exclude lines are
# belt-and-braces so a stray .DS_Store from Finder cannot ride along either.
COPYFILE_DISABLE=1 tar \
  --no-xattrs \
  --exclude='pilot_app/__pycache__' \
  --exclude='pilot_app/tests/__pycache__' \
  --exclude='pilot_app/.env' \
  --exclude='pilot_app/*.sqlite3' \
  --exclude='._*' \
  --exclude='*/._*' \
  --exclude='.DS_Store' \
  --exclude='*/.DS_Store' \
  -czf "$ROOT_DIR/dist/$ARCHIVE" \
  -C "$ROOT_DIR" pilot_app "${EXTRA[@]:-}"
(
  cd "$ROOT_DIR/dist"
  shasum -a 256 "$ARCHIVE" > "$ARCHIVE.sha256"
)
echo "$ROOT_DIR/dist/$ARCHIVE"
echo "校验方式：cd $ROOT_DIR/dist && shasum -a 256 -c $ARCHIVE.sha256"
