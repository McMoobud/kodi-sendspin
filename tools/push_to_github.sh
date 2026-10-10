#!/bin/bash
# One-time: create an EMPTY private repo github.com/Mcmoobud/kodi-sendspin and add this
# machine's public key (~/.ssh/id_ed25519.pub) as a deploy key WITH write access
# (repo Settings -> Deploy keys). Then run this script.
set -e
cd "$(dirname "$0")/.."
git remote get-url origin >/dev/null 2>&1 || git remote add origin git@github.com:Mcmoobud/kodi-sendspin.git
git push -u origin main
