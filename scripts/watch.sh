#!/usr/bin/env bash
# Watches the source repositories and starts the backup the moment one changes.
#
# GitHub runs this repository's schedule only every few hours, so pushes piled
# up and their alerts arrived together. This loop polls every POLL seconds:
#   - each source's refs (branches, tags, PR heads) with git ls-remote
#   - the backend's newest issue/PR update time (its posts are backed up too)
# and on a change dispatches backup.yml with from_watch=true, which backs up
# both sources but stays silent for the one that did not change.
#
# One watcher at a time. A watcher runs LIFETIME seconds, then dispatches its
# successor (handoff=true) and keeps polling until the successor is running, so
# no change falls in the gap. A non-handoff start (schedule, manual) exits at
# once when a watcher is already running; the schedule only restarts a broken
# chain.
set -euo pipefail

POLL="${POLL:-30}"
LIFETIME="${LIFETIME:-20400}" # 5 h 40 min; the job timeout is 6 h
WORKFLOW=watch.yml

others() { # in-progress watcher runs other than this one, newest first
  gh run list -R "$REPO" -w "$WORKFLOW" -s in_progress -L 20 \
    --json databaseId -q ".[].databaseId | select(. != ${RUN_ID})"
}

if [ "${HANDOFF:-false}" != "true" ] && [ -n "$(others)" ]; then
  echo "감시가 이미 실행 중이라 종료해요."
  exit 0
fi

# label|source repo|watch posts too
SOURCES=(
  "백엔드|SYU-Taxi-Delivery/taxi-delivery-sharing|yes"
  "프론트|SYU-Taxi-Delivery/taxi-delivery-sharing-fe|no"
)
GIT_BASE="${GIT_BASE:-https://github.com}" # tests point this at local repos

snapshot() { # $1 repo, $2 posts: one line that changes whenever the source does
  local refs posts=""
  refs="$(git ls-remote "$GIT_BASE/$1.git" | sha256sum | cut -c1-16)" || return 1
  if [ "$2" = yes ]; then
    posts="$(gh api "repos/$1/issues?state=all&sort=updated&direction=desc&per_page=1" \
      -q '.[0].updated_at // ""')" || return 1
  fi
  echo "$refs $posts"
}

last=() # same order as SOURCES
for i in "${!SOURCES[@]}"; do
  IFS='|' read -r label repo posts <<<"${SOURCES[$i]}"
  last[i]="$(snapshot "$repo" "$posts")"
  echo "$(date -u +%H:%M:%S) 기준 [$label] ${last[i]}"
done

start=$SECONDS
handed_off=""
while :; do
  sleep "$POLL"
  for i in "${!SOURCES[@]}"; do
    IFS='|' read -r label repo posts <<<"${SOURCES[$i]}"
    # A failed read (network, rate limit) is skipped, never taken as a change.
    now="$(snapshot "$repo" "$posts")" || { echo "[$label] 확인 실패, 다음에 다시"; continue; }
    if [ "$now" != "${last[i]}" ]; then
      echo "$(date -u +%H:%M:%S) [$label] 변경 감지 → 백업 실행"
      if gh workflow run backup.yml -R "$REPO" -f from_watch=true; then
        last[i]="$now"
      fi
    fi
  done

  if [ -z "$handed_off" ] && [ $((SECONDS - start)) -ge "$LIFETIME" ]; then
    gh workflow run "$WORKFLOW" -R "$REPO" -f handoff=true && handed_off=$SECONDS
    echo "다음 감시를 시작했어요."
  fi
  if [ -n "$handed_off" ]; then
    newer="$(others | awk -v me="$RUN_ID" '$1 > me' | head -1)"
    if [ -n "$newer" ] || [ $((SECONDS - handed_off)) -ge 600 ]; then
      echo "다음 감시($newer)에 넘기고 종료해요."
      exit 0
    fi
  fi
done
