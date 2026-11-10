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
# - Frozen branches (FROZEN, default "main") are never written by this
#   script, so an overwrite can never reach them. Their source is followed
#   in source/<name> instead, with the same archive rule.
set -euo pipefail

: "${SRC_URL:?SRC_URL is required}"
: "${DST_URL:?DST_URL is required}"
FROZEN="${FROZEN:-main}"
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
# SUMMARY_FILE (optional): human-readable lines for a notification.
note() { [ -n "${SUMMARY_FILE:-}" ] && printf '%s\n' "$*" >> "$SUMMARY_FILE"; return 0; }
commits() { git log --no-merges --format='  - %an: %s' -n 5 "$@" 2>/dev/null; }

# sync <source ref> <backup name, e.g. heads/main or tags/v1> [archive name]
sync() {
  local src_ref="$1" name="$2"
  local new old kind short
  new="$(git rev-parse "$src_ref")"
  old="$(git rev-parse -q --verify "refs/dst/$name" || true)"
  kind="${name%%/*}"
  short="${3:-${name#*/}}"
  if [ -z "$old" ]; then
    pushes+=("+$new:refs/$name"); log "new      $name $new"
    note "새 ${short} ($(git log -1 --format='%an: %s' "$new"))"
    return
  fi
  [ "$old" = "$new" ] && return
  if [ "$kind" = "heads" ] && git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
    pushes+=("$new:refs/$name"); log "forward  $name ${old:0:7}..${new:0:7}"
    note "업데이트 ${short}: $(git rev-list --count "$old..$new")개 커밋"
    note "$(commits "$old..$new")"
    return
  fi
  # Overwritten: keep what the backup had, then follow the source.
  archives+=("$old:refs/$kind/archive/$short/$STAMP")
  pushes+=("+$new:refs/$name")
  log "ARCHIVED $name ${old:0:7} -> $kind/archive/$short/$STAMP, now ${new:0:7}"
  note "[주의] ${short}이(가) 덮어써졌어요. 이전 상태는 archive/${short}/${STAMP}에 보관했어요."
  note "  지금: $(git log -1 --format='%an: %s' "$new")"
}

frozen() { case " $FROZEN " in *" $1 "*) return 0 ;; esac; return 1; }

while read -r ref; do
  b="${ref#refs/src/heads/}"
  if frozen "$b"; then sync "$ref" "heads/source/$b" "$b"; else sync "$ref" "heads/$b"; fi
done \
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
    note "[실패] ${spec##*:} 백업에 올리지 못했어요."
  fi
done
log "pushed $(( ${#pushes[@]} - failed )) of ${#pushes[@]} ref update(s)"
[ "$failed" -eq 0 ]
