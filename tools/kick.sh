#!/bin/bash
# Mac launchd kicker (com.tars.aiactivist-cloudkick, every 10 min). GitHub's own cron skips most
# runs (3 of 144 ran on 10/30, so every X video went stale). This dispatches the post workflow so
# it runs on time while the Mac is awake. The GitHub cron stays as the backup when the Mac is off.
# Safe to double-fire: the workflow's concurrency group never overlaps runs.
export PATH="/opt/homebrew/bin:/usr/bin:/bin"
REPO="KidRain1000/aiactivist-cloudpost"
busy="$(gh run list -R "$REPO" -w post -L 5 --json status -q '[.[] | select(.status != "completed")] | length' 2>&1)"
if [ "$busy" != "0" ]; then echo "$(date '+%F %T') skip: busy=$busy"; exit 0; fi
gh workflow run post -R "$REPO" && echo "$(date '+%F %T') dispatched"
