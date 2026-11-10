#!/usr/bin/env bash
# Mirror every branch, tag and PR head of SRC_URL into DST_URL without ever
# losing anything already in the backup.
#
# - A branch or PR head that moved forward is updated.
# - Before the backup is changed in a way that could hide its current state,
#   that state is kept as a recovery ref and the ref is verified on the
#   backup. This happens when:
#     * a branch, PR head or tag was overwritten (force push, history replaced),
#     * a forward update deletes files,
#     * a branch or tag was deleted in the source (the backup ref itself is
#       also left in place).
#   Recovery refs: archive/<name>/<UTC time>[-deleted] (branches) and
#   tags/archive/<name>/<UTC time>[-deleted] (tags).
# - If keeping or verifying that state fails, the backup ref is left as it is
#   (the update is held) and the run fails, so it is retried and reported.
# - Nothing is deleted. Backup-only refs (backup-bot, archive/*) are never
#   touched.
# - Frozen branches (FROZEN, default "main") are never written by this
#   script. Their source is followed in source/<name> instead.
set -euo pipefail

: "${SRC_URL:?SRC_URL is required}"
: "${DST_URL:?DST_URL is required}"
FROZEN="${FROZEN:-main}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%MZ)}"
# WORK_DIR (optional): keep the fetched repository there for the report step.
if [ -n "${WORK_DIR:-}" ]; then
  WORK="$WORK_DIR"; mkdir -p "$WORK"
else
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
fi
cd "$WORK"
git init -q --bare .
git fetch -q "$SRC_URL" '+refs/heads/*:refs/src/heads/*' '+refs/tags/*:refs/src/tags/*' '+refs/pull/*/head:refs/src/pull/*'
git fetch -q "$DST_URL" '+refs/heads/*:refs/dst/heads/*' '+refs/tags/*:refs/dst/tags/*'

log() { printf '%s\n' "$*"; }
# SUMMARY_FILE (optional): human-readable lines (kept for older readers).
note() { [ -n "${SUMMARY_FILE:-}" ] && printf '%s\n' "$*" >> "$SUMMARY_FILE"; return 0; }
# TRANSITIONS_FILE (optional): TSV lines read by scripts/report.py.
record() { [ -n "${TRANSITIONS_FILE:-}" ] && printf '%s\n' "$(IFS=$'\t'; echo "$*")" >> "$TRANSITIONS_FILE"; return 0; }

# Planned work, one entry per backup ref: "name|new|archive_ref|old"
# (new is empty for a deleted ref; archive empty when nothing to keep).
plans=()

# plan <source ref> <backup name, e.g. heads/main or tags/v1> [archive name]
plan() {
  local src_ref="$1" name="$2"
  local new old kind short archive="" what
  new="$(git rev-parse "$src_ref")"
  old="$(git rev-parse -q --verify "refs/dst/$name" || true)"
  kind="${name%%/*}"
  short="${3:-${name#*/}}"
  if [ -z "$old" ]; then
    what=new
  elif [ "$old" = "$new" ]; then
    return
  elif [ "$kind" = "heads" ] && git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
    if [ -n "$(git diff --name-only --diff-filter=D "$old" "$new")" ]; then
      what=forward-deletes; archive="$kind/archive/$short/$STAMP"
    else
      what=forward
    fi
  else
    what=overwrite; archive="$kind/archive/$short/$STAMP"
  fi
  plans+=("$name|$new|$archive|$old")
  record change "$src_ref" "$name" "${old:--}" "$new" "${archive:--}" "$what"
  log "$what $name ${old:0:7} -> ${new:0:7}${archive:+ (keep as $archive)}"
}

frozen() { case " $FROZEN " in *" $1 "*) return 0 ;; esac; return 1; }

while read -r ref; do
  b="${ref#refs/src/heads/}"
  if frozen "$b"; then plan "$ref" "heads/source/$b" "$b"; else plan "$ref" "heads/$b"; fi
done < <(git for-each-ref --format='%(refname)' refs/src/heads)
while read -r ref; do plan "$ref" "tags/${ref#refs/src/tags/}"; done \
  < <(git for-each-ref --format='%(refname)' refs/src/tags)
while read -r ref; do plan "$ref" "heads/pr/${ref#refs/src/pull/}"; done \
  < <(git for-each-ref --format='%(refname)' refs/src/pull)

# Branches and tags deleted in the source: keep them once as a recovery ref.
while read -r ref sha; do
  name="${ref#refs/dst/}"; kind="${name%%/*}"; short="${name#*/}"
  case "$short" in backup-bot|archive/*|pr/*|token-check/*) continue ;; esac
  if [ "$kind" = heads ]; then
    case "$short" in
      source/*) frozen "${short#source/}" || continue
                short="${short#source/}"; src="refs/src/heads/$short" ;;
      *) frozen "$short" && continue; src="refs/src/heads/$short" ;;
    esac
  else
    src="refs/src/tags/$short"
  fi
  git rev-parse -q --verify "$src" >/dev/null && continue
  # Already kept by an earlier run?
  if git for-each-ref --format='%(objectname)' "refs/dst/$kind/archive/$short/" | grep -qx "$sha"; then
    continue
  fi
  archive="$kind/archive/$short/$STAMP-deleted"
  plans+=("$name||$archive|$sha")
  record change "$src" "$name" "$sha" - "$archive" deleted
  log "deleted $name ${sha:0:7} (keep as $archive)"
done < <(git for-each-ref --format='%(refname) %(objectname)' refs/dst/heads refs/dst/tags)

if [ "${#plans[@]}" -eq 0 ]; then
  log "no changes"
  exit 0
fi

# 1) Keep every state that is about to be hidden, and check on the backup
#    that the recovery ref really points at it.
kept_ok=" "
for p in "${plans[@]}"; do
  IFS='|' read -r name new archive old <<< "$p"
  [ -z "$archive" ] && continue
  if git push -q "$DST_URL" "$old:refs/$archive" 2>push.err &&
     [ "$(git ls-remote "$DST_URL" "refs/$archive" | cut -f1)" = "$old" ]; then
    kept_ok+="$archive "
    record archived "$archive" "$old" ok
    log "kept     $archive ${old:0:7}"
  else
    record archived "$archive" "$old" failed
    log "KEEP FAILED $archive ($(tr '\n' ' ' < push.err | cut -c1-160))"
    note "[실패] ${name#*/}의 이전 상태를 보관하지 못해 백업 갱신을 보류했어요."
  fi
done

# 2) Follow the source one ref at a time. A ref whose previous state could
#    not be kept is held as it is. Any hold or refusal fails the run.
failed=0
for p in "${plans[@]}"; do
  IFS='|' read -r name new archive old <<< "$p"
  [ -z "$new" ] && continue   # deleted in the source: the backup ref stays
  if [ -n "$archive" ] && [ "${kept_ok#* $archive }" = "$kept_ok" ]; then
    failed=$((failed + 1)); record pushed "refs/$name" held
    log "HELD     $name (previous state not kept)"
    continue
  fi
  force=""
  if [ -n "$archive" ] || [ -z "$old" ]; then force="+"; fi
  if git push -q "$DST_URL" "$force$new:refs/$name" 2>push.err; then
    record pushed "refs/$name" ok
  else
    failed=$((failed + 1)); record pushed "refs/$name" refused
    log "REFUSED  $name ($(tr '\n' ' ' < push.err | cut -c1-160))"
    note "[실패] $name 백업에 올리지 못했어요."
  fi
done
log "done, $failed held or refused"
[ "$failed" -eq 0 ]
