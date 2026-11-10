#!/usr/bin/env bash
# Mirror every branch, tag and PR head of SRC_URL into DST_URL without ever
# losing anything already in the backup.
#
# - A branch or PR head that moved forward is updated.
# - A branch, PR head or tag that was overwritten (force push, history
#   replaced) first keeps its old commit as archive/<name>/<UTC time>, then
#   takes the new commit.
# - Nothing is deleted: a branch deleted in the source stays in the backup.
# - Backup-only refs (backup-bot, archive/*) are never touched.
set -euo pipefail

: "${SRC_URL:?SRC_URL is required}"
: "${DST_URL:?DST_URL is required}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%MZ)}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
git init -q --bare .
git fetch -q "$SRC_URL" '+refs/heads/*:refs/src/heads/*' '+refs/tags/*:refs/src/tags/*' '+refs/pull/*/head:refs/src/pull/*'
git fetch -q "$DST_URL" '+refs/heads/*:refs/dst/heads/*' '+refs/tags/*:refs/dst/tags/*'

archives=()
pushes=()
log() { printf '%s\n' "$*"; }

# sync <source ref> <backup name, e.g. heads/main or tags/v1>
sync() {
  local src_ref="$1" name="$2"
  local new old kind short
  new="$(git rev-parse "$src_ref")"
  old="$(git rev-parse -q --verify "refs/dst/$name" || true)"
  kind="${name%%/*}"
  short="${name#*/}"
  if [ -z "$old" ]; then
    pushes+=("+$new:refs/$name"); log "new      $name $new"
    return
  fi
  [ "$old" = "$new" ] && return
  if [ "$kind" = "heads" ] && git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
    pushes+=("$new:refs/$name"); log "forward  $name ${old:0:7}..${new:0:7}"
    return
  fi
  # Overwritten: keep what the backup had, then follow the source.
  archives+=("$old:refs/$kind/archive/$short/$STAMP")
  pushes+=("+$new:refs/$name")
  log "ARCHIVED $name ${old:0:7} -> $kind/archive/$short/$STAMP, now ${new:0:7}"
}

while read -r ref; do sync "$ref" "heads/${ref#refs/src/heads/}"; done \
  < <(git for-each-ref --format='%(refname)' refs/src/heads)
while read -r ref; do sync "$ref" "tags/${ref#refs/src/tags/}"; done \
  < <(git for-each-ref --format='%(refname)' refs/src/tags)
while read -r ref; do sync "$ref" "heads/pr/${ref#refs/src/pull/}"; done \
  < <(git for-each-ref --format='%(refname)' refs/src/pull)

if [ "${#pushes[@]}" -eq 0 ]; then
  log "no changes"
  exit 0
fi

# 1) Keep every overwritten state first, all or nothing.
if [ "${#archives[@]}" -gt 0 ]; then
  git push -q --atomic "$DST_URL" "${archives[@]}"
  log "archived ${#archives[@]} overwritten ref(s)"
fi

# 2) Then follow the source one ref at a time, so one refused ref (for
#    example a commit that edits workflow files, which GITHUB_TOKEN may not
#    push) never blocks the others. Any refusal fails the run visibly.
failed=0
for spec in "${pushes[@]}"; do
  if ! git push -q "$DST_URL" "$spec" 2>push.err; then
    failed=$((failed + 1))
    log "REFUSED  ${spec##*:} ($(tr '\n' ' ' < push.err | cut -c1-160))"
  fi
done
log "pushed $(( ${#pushes[@]} - failed )) of ${#pushes[@]} ref update(s)"
[ "$failed" -eq 0 ]
