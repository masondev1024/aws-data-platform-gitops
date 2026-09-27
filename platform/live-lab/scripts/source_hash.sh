#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

mkdir -p platform/live-lab/evidence

tracked_tree="$(git rev-parse HEAD^{tree})"
tracked_diff="$(git diff --binary HEAD -- . | git hash-object --stdin)"
untracked_listing="$(git ls-files --others --exclude-standard | LC_ALL=C sort | sed '/^$/d')"
untracked_hash="$(
  if [ -n "$untracked_listing" ]; then
    while IFS= read -r path; do
      [ -f "$path" ] || continue
      git hash-object "$path"
      printf '  %s\n' "$path"
    done <<< "$untracked_listing" | git hash-object --stdin
  else
    printf 'no-untracked' | git hash-object --stdin
  fi
)"

{
  printf 'head=%s\n' "$(git rev-parse HEAD)"
  printf 'tracked_tree=%s\n' "$tracked_tree"
  printf 'tracked_diff_hash=%s\n' "$tracked_diff"
  printf 'untracked_hash=%s\n' "$untracked_hash"
} | tee platform/live-lab/evidence/source-hash.txt
