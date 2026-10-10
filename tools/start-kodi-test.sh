#!/bin/bash
# Stop any running Kodi completely before starting a fresh one (two instances = duplicate players)
pkill -TERM -x kodi.bin
for i in $(seq 1 30); do pgrep -x kodi.bin >/dev/null || break; sleep 0.5; done
pkill -KILL -x kodi.bin; pkill -x Xvfb; sleep 1
export XDG_RUNTIME_DIR=/run/user/0; mkdir -p $XDG_RUNTIME_DIR
if ! pactl info >/dev/null 2>&1; then
  pulseaudio -D --exit-idle-time=-1 --log-target=file:/tmp/pulse.log; sleep 1
  pactl load-module module-null-sink sink_name=silent sink_properties=device.description=Silent >/dev/null
  pactl set-default-sink silent
fi
Xvfb :1 -screen 0 1280x720x24 >/tmp/xvfb.log 2>&1 &
sleep 2
mkdir -p /tmp/tl; rm -f /tmp/tl/*; DISPLAY=:1 SENDSPIN_TIMELINE_DIR=/tmp/tl KODI_AE_SINK=PULSE LIBGL_ALWAYS_SOFTWARE=1 nohup kodi --standalone >/tmp/kodi-stdout.log 2>&1 &
