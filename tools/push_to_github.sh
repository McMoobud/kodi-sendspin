#!/bin/bash
# Push main to github.com/McMoobud/kodi-sendspin over HTTPS.
# Needs a GitHub token with Contents: write in $GITHUB_TOKEN (never stored in the repo).
set -e
cd "$(dirname "$0")/.."
: "${GITHUB_TOKEN:?set GITHUB_TOKEN}"
git remote get-url origin >/dev/null 2>&1 || git remote add origin https://github.com/McMoobud/kodi-sendspin.git
git -c credential.helper= -c "credential.helper=!f() { echo username=x-access-token; echo password=\$GITHUB_TOKEN; }; f" push -u origin main
