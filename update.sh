#!/usr/bin/env bash
# Point sources.json at the newest stable Grok Bot release.
#
# Asks Cursor's update service for the latest Windows installer, records its
# version, URL and hash, and reads which Electron major it was built with.
#
# Usage: nix develop -c ./update.sh [--force]
set -euo pipefail

cd "$(dirname "$0")"

force=0
[ "${1:-}" = "--force" ] && force=1

for cmd in curl jq 7zz nix; do
  command -v "$cmd" > /dev/null || {
    echo "missing $cmd; run this inside 'nix develop'" >&2
    exit 1
  }
done

# The service wants a machine id but serves the same release to any of them.
machine_id=$(cat /proc/sys/kernel/random/uuid)
release=$(curl -fsSL --retry 3 --max-time 30 \
  "https://api2.cursor.sh/updates/api/update/win32-x64-user/sand/0.0.0/${machine_id}/stable")

version=$(jq -r '.version // empty' <<< "$release")
url=$(jq -r '.url // empty' <<< "$release")

if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "unexpected version in update response: '$version'" >&2
  exit 1
fi
expected="https://downloads.cursor.com/grokbot/stable/win32-x64/${version}/Grok_Bot_${version}_Setup.exe"
if [ "$url" != "$expected" ]; then
  echo "unexpected installer URL: $url" >&2
  exit 1
fi

current=$(jq -r .version sources.json)
if [ "$current" = "$version" ] && [ "$force" = 0 ]; then
  echo "already at $version"
  exit 0
fi

echo "fetching $url"
prefetch=$(nix store prefetch-file --json "$url")
hash=$(jq -r .hash <<< "$prefetch")
exe=$(jq -r .storePath <<< "$prefetch")

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
7zz x -y -o"$tmp" "$exe" '$PLUGINSDIR/app-64.7z' > /dev/null
7zz x -y -o"$tmp" "$tmp/\$PLUGINSDIR/app-64.7z" 'Grok Bot.exe' > /dev/null
electron=$(grep -aoE -m1 'Electron/[0-9]+\.[0-9]+\.[0-9]+' "$tmp/Grok Bot.exe" | cut -d/ -f2)
electron=${electron%%$'\n'*}

jq -n --arg version "$version" --arg url "$url" --arg hash "$hash" \
  --arg electron "${electron%%.*}" \
  '{version: $version, url: $url, hash: $hash, electron: $electron}' > sources.json

echo "Grok Bot $current -> $version (built for Electron $electron)"
echo "next: nix build, then run it"
