"""Protocol probe: legacy (unencrypted) Sendspin player client. Discards audio."""
import asyncio, json, struct, sys, time, aiohttp
URL = sys.argv[1] if len(sys.argv) > 1 else "ws://192.168.1.10:8927/sendspin"
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 60
def now_us(): return time.monotonic_ns() // 1000
async def main():
    stats = {"bytes": 0, "chunks": 0, "types": {}}
    async with aiohttp.ClientSession() as s, s.ws_connect(URL, heartbeat=None, max_msg_size=0) as ws:
        hello = {"type": "client/hello", "payload": {
            "client_id": "kodi-sendspin-dev", "name": "Kodi Sendspin (dev)", "version": 1,
            "supported_roles": ["player@v1", "metadata@v1"],
            "device_info": {"product_name": "Kodi Sendspin", "manufacturer": "kodi-sendspin", "software_version": "0.0.1"},
            "player@v1_support": {"supported_formats": [{"codec": "pcm", "channels": 2, "sample_rate": 48000, "bit_depth": 16},
                                                        {"codec": "pcm", "channels": 2, "sample_rate": 44100, "bit_depth": 16}],
                                  "buffer_capacity": 2_000_000, "supported_commands": ["volume", "mute"]}}}
        await ws.send_str(json.dumps(hello))
        async def timesync():
            while True:
                await ws.send_str(json.dumps({"type": "client/time", "payload": {"client_transmitted": now_us()}}))
                await asyncio.sleep(2)
        t = asyncio.create_task(timesync())
        end = time.time() + DUR
        while time.time() < end:
            try: msg = await asyncio.wait_for(ws.receive(), timeout=end - time.time())
            except asyncio.TimeoutError: break
            if msg.type == aiohttp.WSMsgType.TEXT:
                d = json.loads(msg.data); typ = d.get("type")
                if typ == "server/time":
                    p = d["payload"]; rx = now_us()
                    off = ((p["server_received"] - p["client_transmitted"]) + (p["server_transmitted"] - rx)) / 2
                    rtt = (rx - p["client_transmitted"]) - (p["server_transmitted"] - p["server_received"])
                    if stats["types"].get(typ, 0) < 3: print(f"server/time offset={off:.0f}us rtt={rtt}us")
                else:
                    print("TEXT", json.dumps(d)[:600])
                stats["types"][typ] = stats["types"].get(typ, 0) + 1
                if typ == "server/hello":
                    await ws.send_str(json.dumps({"type": "client/state", "payload": {"player": {"state": "synchronized", "volume": 20, "muted": False}}}))
                    print(">> sent client/state")
            elif msg.type == aiohttp.WSMsgType.BINARY:
                b = msg.data; mtype = b[0]; ts = struct.unpack(">q", b[1:9])[0]
                stats["chunks"] += 1; stats["bytes"] += len(b) - 9
                if stats["chunks"] <= 3 or stats["chunks"] % 200 == 0:
                    print(f"BIN type={mtype} ts={ts} len={len(b)-9} lead_vs_now={(ts - now_us())/1000:.0f}ms(raw clock)")
            else:
                print("WS", msg.type, msg.data); break
        t.cancel()
    print("STATS", stats)
asyncio.run(main())
