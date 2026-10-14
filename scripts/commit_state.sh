#!/usr/bin/env bash
# Commit state/ back to the default branch. Safe to call when nothing changed.
# Usage: scripts/commit_state.sh "<message prefix>"
set -euo pipefail

BRANCH="${GITHUB_REF_NAME:-main}"
git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add state/
if git diff --cached --quiet; then
  echo "No state changes to commit."
  exit 0
fi

# [skip ci] stops any push-triggered workflows from running on bot commits.
git commit -m "$1 $(TZ=America/New_York date '+%Y-%m-%d %H:%M %Z') [skip ci]"

for attempt in 1 2 3 4; do
  if git pull --rebase origin "$BRANCH" && git push origin "HEAD:$BRANCH"; then
    echo "State pushed."
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  sleep $((2 ** attempt))
done
echo "::error::Failed to push state after 4 attempts." >&2
exit 1
