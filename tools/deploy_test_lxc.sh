#!/bin/bash
# Deploy the add-on into the throwaway Kodi test container (a Proxmox LXC; set PVE_HOST and CTID)
# and restart Kodi there. The container has no sound hardware (PulseAudio null sink).
set -e
cd "$(dirname "$0")/.."
tar czf /tmp/service.sendspin.tgz --exclude=__pycache__ service.sendspin
scp -q -o BatchMode=yes /tmp/service.sendspin.tgz root@"$PVE_HOST":/tmp/
ssh -o BatchMode=yes root@"$PVE_HOST" "pct push $CTID /tmp/service.sendspin.tgz /tmp/service.sendspin.tgz && pct exec $CTID -- bash -c '
rm -rf /root/.kodi/addons/service.sendspin && tar xzf /tmp/service.sendspin.tgz -C /root/.kodi/addons/ && /root/start-kodi.sh'"
echo deployed
