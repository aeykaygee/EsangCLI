"""SuperFit / Gymax / GoPlus EsangLinker treadmill CLI (Windows + Bleak).
Service 0xFFF0, Notify 0xFFF1, Write 0xFFF2.
Protocol reverse-engineered from com.costway.gymax decompile + qdomyos-zwift #2628.
Frames: A9 ... + XOR checksum (xor of all previous bytes).
"""
import argparse
import asyncio
import sys
from bleak import BleakScanner, BleakClient

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
TARGET_NAMES = ["esanglinker", "eslinker", "esbrlinker", "eslinkerhr"]

def xor_checksum(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c

def frame(hex_base: str) -> bytes:
    raw = bytes.fromhex(hex_base)
    return raw + bytes([xor_checksum(raw)])

def speed_frame(speed_raw: int) -> bytes:
    if not 0 <= speed_raw <= 150:
        raise ValueError("speed_raw must be 0-150")
    return frame(f"a90101{speed_raw:02x}")

START_FRAME = frame("a9a30101")  # -> a9a30101 0a
STOP_FRAME = frame("a9a30100")   # -> a9a30100 0b
# Poll queries (base without xor; frame() adds xor)
QUERY_NOWSPEED = frame("a9ae01fe")
QUERY_DEVICE = frame("a91e01fe")
QUERY_RUNID = frame("a9b201fe")
QUERY_TYPE = frame("a9b001fe")
HEARTBEAT_1 = frame("a9020d")
HEARTBEAT_2 = frame("a9020e")
GET_RUN_HISTORY = frame("a9fa0101")
GET_RUNID_1 = frame("a9f505")
GET_RUNTYPE = frame("a9f601")

def query_speed_frame(counter: int) -> bytes:
    return frame(f"a90a01{counter & 0xFF:02x}")

def kmh_to_raw(kmh: float) -> int:
    # Observed: byte = speed in 0.1 km/h (e.g. 0x09 = 0.9 km/h, max 150 = 15.0)
    # Verify on your machine starting low!
    return int(round(kmh * 10))

async def cmd_scan(timeout: float = 8.0):
    print(f"Scanning {timeout}s for BLE devices (look for EsangLinker)...")
    devices = await BleakScanner.discover(timeout=timeout)
    found = []
    for d in devices:
        name = d.name or ""
        mark = " <-- LIKELY TREADMILL" if name.lower() in TARGET_NAMES or "linker" in name.lower() else ""
        print(f"  {name} [{d.address}] RSSI={getattr(d, 'rssi', '?')}{mark}")
        if mark:
            found.append(d)
    if not found:
        print("\nNo EsangLinker found. Tips: treadmill on? Long-press '-' 3s until 'Di'? Close Gymax app? Move PC closer?")
    return found

async def find_device(address: str | None):
    if address:
        return address
    print("Auto-searching for EsangLinker...")
    devices = await BleakScanner.discover(timeout=8.0)
    for d in devices:
        if (d.name or "").lower() in TARGET_NAMES or "linker" in (d.name or "").lower():
            print(f"Found {d.name} [{d.address}]")
            return d.address
    print("Not found. Run 'scan' first and pass --address explicitly.")
    return None

def decode_history_payload(payload_hex: str) -> dict:
    """Payload after a9af10 (16 bytes hex). Confirmed by 3 captures:
    bytes[4:6] LE = distance meters (11, 100, 34)
    bytes[8:10] BE = time seconds +3s delay (22, 124, 63)
    """
    b = bytes.fromhex(payload_hex)
    if len(b) < 12:
        return {}
    dist_m = int.from_bytes(b[4:6], 'little')
    time_s = int.from_bytes(b[8:10], 'big')
    run_idx = b[1] if len(b) > 1 else -1
    return {"run": run_idx, "distance_m": dist_m, "time_s": time_s,
            "time_s_adj": max(0, time_s - 3)}

def decode_frame(hx: str) -> str:
    # Known from com.costway.gymax decompile + live captures
    try:
        if hx.startswith("a90801"):
            return "handshake challenge (ignore)"
        if hx.startswith("a9e001"):
            raw = int(hx[6:8], 16)
            return f"SPEED echo: raw={raw} ~{raw/10:.1f} km/h"
        if hx.startswith("a9f20301"):
            raw = int(hx[8:10], 16)
            return f"unit/speed info: raw={raw} ~{raw/10:.1f} km/h"
        if hx.startswith("a90901"):
            st = int(hx[6:8], 16)
            states = {0: "stopped", 1: "running?", 5: "stopping?", 6: "stopping?"}
            return f"STATE: {st} ({states.get(st, 'unknown')})"
        if hx.startswith("a90a04"):
            return f"QUERY reply (speed/device data): {hx[6:-2]}"
        if hx.startswith("a9ae"):
            return f"NOWSPEED query echo: {hx[4:-2]}"
        if hx.startswith("a9f401"):
            raw = int(hx[6:8], 16)
            return f"LIVE SPEED: raw={raw} ~{raw/10:.1f} km/h"
        if hx.startswith("a9fe01"):
            return f"RUN status: {hx[6:-2]}"
        if hx.startswith("a91e0c"):
            return f"DEVICE info (static): {hx[6:-2]}"
        if hx.startswith("a9f506"):
            return f"RUNID: {hx[6:-2]} (session id)"
        if hx.startswith("a9f601"):
            return f"RUNTYPE: {hx[6:-2]}"
        if hx.startswith("a9f4"):
            return f"NOWSPEED/stats: {hx[6:-2]}"
        if hx.startswith("a902"):
            return f"HEARTBEAT reply: {hx[6:-2]}"
        if hx.startswith("a91e"):
            return f"DEVICE reply: {hx[6:-2]}"
        if hx.startswith("a9b2"):
            return f"RUNID reply: {hx[6:-2]}"
    except Exception:
        pass
    return ""

def make_notify_handler(logfile=None, quiet_handshake=True):
    def cb(_, data: bytearray):
        hx = data.hex()
        if quiet_handshake and hx.startswith("a90801"):
            if logfile:
                logfile.write(hx + "\n")
            return  # skip spam
        print(f"  NOTIFY {hx}", flush=True)
        if logfile:
            logfile.write(hx + "\n")
            logfile.flush()
        # best-effort decode of known prefixes
        if hx.startswith("a9"):
            ok = xor_checksum(data[:-1]) == data[-1]
            dec = decode_frame(hx)
            print(f"    -> xor_ok={ok} {dec}")
    return cb

async def cmd_monitor(address, duration):
    addr = await find_device(address)
    if not addr:
        return
    async with BleakClient(addr) as client:
        print(f"Connected to {addr}. Subscribing to {NOTIFY_UUID}...")
        with open("treadmill_notify.log", "a") as lf:
            await client.start_notify(NOTIFY_UUID, make_notify_handler(lf))
            print(f"Logging to treadmill_notify.log for {duration}s. Belt state changes should stream here.")
            print("NOTE: Gymax app must be closed (1 connection max).")
            await asyncio.sleep(duration)
            await client.stop_notify(NOTIFY_UUID)
        print("Done.")

async def cmd_history(address, listen: float = 8.0):
    addr = await find_device(address)
    if not addr:
        return
    from collections import deque
    import time
    chunks = []
    def cb(_, data: bytearray):
        hx = data.hex()
        chunks.append((time.time(), hx))
        print(f"  NOTIFY {hx} len={len(data)}")
    async with BleakClient(addr) as client:
        print(f"Connected to {addr}.")
        await client.start_notify(NOTIFY_UUID, cb)
        q = frame("a9fa0101")
        print(f"Sending HISTORY: {q.hex()}")
        await client.write_gatt_char(WRITE_UUID, q, response=False)
        await asyncio.sleep(listen)
        await client.stop_notify(NOTIFY_UUID)
        print("Done.")
        if chunks:
            print("\nCombined (in order):")
            for _, hx in chunks:
                print(f"  {hx}")
            # try decode a9af10... as 16-bit BE/LE candidates
            for _, hx in chunks:
                if hx.startswith("a9af10"):
                    raw = hx[6:]
                    print(f"\nHistory payload: {raw}")
                    d = decode_history_payload(raw)
                    if d:
                        print(f"  Decoded: run={d['run']} distance={d['distance_m']}m ({d['distance_m']/1000:.2f}km) time={d['time_s']}s (~{d['time_s_adj']}s adj)")

async def cmd_status(address):
    addr = await find_device(address)
    if not addr:
        return
    results = {}
    def cb(_, data: bytearray):
        hx = data.hex()
        if hx.startswith("a90801"):
            return
        print(f"  NOTIFY {hx} {decode_frame(hx)}")
        if hx.startswith("a9f401") and len(hx) >= 8:
            try:
                results["speed_raw"] = int(hx[6:8], 16)
            except Exception:
                pass
        if hx.startswith("a9af10"):
            d = decode_history_payload(hx[6:])
            if d:
                results.update(d)
    async with BleakClient(addr) as client:
        print(f"Connected to {addr}. Querying status...")
        await client.start_notify(NOTIFY_UUID, cb)
        await client.write_gatt_char(WRITE_UUID, QUERY_NOWSPEED, response=False)
        await asyncio.sleep(1.5)
        await client.write_gatt_char(WRITE_UUID, frame("a9fa0101"), response=False)
        await asyncio.sleep(3.0)
        await client.stop_notify(NOTIFY_UUID)
        spd = results.get("speed_raw")
        if spd is not None:
            print(f"\nLive speed: {spd/10:.1f} km/h (raw {spd})")
        if "distance_m" in results:
            print(f"Last run: {results['distance_m']}m in {results['time_s']}s (adj ~{results.get('time_s_adj')}s)")
        if not results:
            print("No telemetry replies (is belt on? try while moving).")
        print("Done.")

async def cmd_write(address, payload: bytes, label: str, listen: float = 4.0):
    addr = await find_device(address)
    if not addr:
        return
    async with BleakClient(addr) as client:
        print(f"Connected to {addr}.")
        try:
            await client.start_notify(NOTIFY_UUID, make_notify_handler())
        except Exception as e:
            print(f"(notify subscribe failed, continuing): {e}")
        print(f"Sending {label}: {payload.hex()}")
        await client.write_gatt_char(WRITE_UUID, payload, response=False)
        print(f"Waiting {listen}s for notifications...")
        await asyncio.sleep(listen)
        print("Done. Check belt / NOTIFY lines above.")

async def cmd_run(address, speed_raw: int):
    addr = await find_device(address)
    if not addr:
        return
    async with BleakClient(addr) as client:
        print(f"Connected to {addr}.")
        try:
            await client.start_notify(NOTIFY_UUID, make_notify_handler())
        except Exception as e:
            print(f"(notify subscribe failed, continuing): {e}")
        print(f"Sending START: {START_FRAME.hex()}")
        await client.write_gatt_char(WRITE_UUID, START_FRAME, response=False)
        await asyncio.sleep(3.0)
        sf = speed_frame(speed_raw)
        print(f"Sending SPEED raw={speed_raw}: {sf.hex()}")
        await client.write_gatt_char(WRITE_UUID, sf, response=False)
        print("Waiting 4s for notifications...")
        await asyncio.sleep(4.0)
        print("Done.")

async def cmd_live(address, duration: float, interval: float = 2.0):
    addr = await find_device(address)
    if not addr:
        return
    async with BleakClient(addr) as client:
        print(f"Connected to {addr}. Polling telemetry for {duration}s...")
        with open("treadmill_notify.log", "a") as lf:
            await client.start_notify(NOTIFY_UUID, make_notify_handler(lf))
            import time, random
            start = time.time()
            counter = random.randint(0, 255)
            polls = [QUERY_NOWSPEED, QUERY_RUNID, QUERY_TYPE, QUERY_DEVICE, HEARTBEAT_1, HEARTBEAT_2]
            i = 0
            while time.time() - start < duration:
                # rotate: every 4th is querySpeed with incrementing counter, else cycle polls
                if i % 4 == 0:
                    q = query_speed_frame(counter)
                    counter = (counter + 1) & 0xFF
                    label = f"POLL querySpeed {q.hex()}"
                else:
                    q = polls[(i % 4 - 1 + (i // 4) * 3) % len(polls)]
                    label = f"POLL {q.hex()}"
                print(label)
                lf.write(f">> {q.hex()}\n")
                try:
                    await client.write_gatt_char(WRITE_UUID, q, response=False)
                except Exception as e:
                    print(f"write failed: {e}")
                i += 1
                await asyncio.sleep(interval)
            await client.stop_notify(NOTIFY_UUID)
        print("Done. See treadmill_notify.log (>> = sent, plain = received).")

async def main_async(args):
    if args.cmd == "scan":
        await cmd_scan(args.timeout)
    elif args.cmd == "monitor":
        await cmd_monitor(args.address, args.time)
    elif args.cmd == "start":
        print("WARNING: belt will start. Stand on side rails, safety key in reach.")
        await cmd_write(args.address, START_FRAME, "START")
    elif args.cmd == "stop":
        await cmd_write(args.address, STOP_FRAME, "STOP")
    elif args.cmd == "raw":
        hx = args.hex.replace(" ", "").lower()
        data = bytes.fromhex(hx)
        if args.add_xor:
            data = data + bytes([xor_checksum(data)])
        print(f"Sending RAW: {data.hex()}")
        await cmd_write(args.address, data, "RAW", listen=args.listen)
    elif args.cmd == "speed":
        raw = args.raw if args.raw is not None else kmh_to_raw(args.kmh)
        print(f"Speed raw={raw} (~{raw/10:.1f} km/h assumed). Start LOW for first test (e.g. 1.0).")
        if raw > 60 and not args.yes:
            print("Refusing raw>60 (>~6km/h) without --yes. Re-run with --yes if belt area is clear.")
            return
        await cmd_write(args.address, speed_frame(raw), f"SPEED raw={raw}")
    elif args.cmd == "run":
        raw = args.raw if args.raw is not None else kmh_to_raw(args.kmh)
        print(f"RUN: start + {raw/10:.1f} km/h. Stand on side rails!")
        if raw > 60 and not args.yes:
            print("Refusing raw>60 without --yes.")
            return
        await cmd_run(args.address, raw)
    elif args.cmd == "live":
        await cmd_live(args.address, args.time, args.interval)
    elif args.cmd == "history":
        await cmd_history(args.address, args.listen)
    elif args.cmd == "status":
        await cmd_status(args.address)

def main():
    p = argparse.ArgumentParser(description="SuperFit/EsangLinker treadmill control")
    p.add_argument("--address", help="BLE MAC of EsangLinker (else auto-find)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan for treadmill")
    s.add_argument("--timeout", type=float, default=8.0)
    m = sub.add_parser("monitor", help="subscribe to telemetry notifications (read-only, safe)")
    m.add_argument("--time", type=float, default=30.0)
    sub.add_parser("start", help="start belt (CAUTION)")
    sub.add_parser("stop", help="stop belt")
    sp = sub.add_parser("speed", help="set speed (CAUTION)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--kmh", type=float, help="e.g. 2.5")
    g.add_argument("--raw", type=int, help="0-150 direct byte, e.g. 10 = ~1.0km/h")
    sp.add_argument("--yes", action="store_true", help="allow high speeds")
    rn = sub.add_parser("run", help="start belt AND set speed in one connection (use from stopped)")
    rg = rn.add_mutually_exclusive_group(required=True)
    rg.add_argument("--kmh", type=float, help="e.g. 2.0")
    rg.add_argument("--raw", type=int, help="0-150 direct")
    rn.add_argument("--yes", action="store_true")
    rp = sub.add_parser("raw", help="send raw hex to FFF2 (for handshake/pairing tests)")
    rp.add_argument("--hex", required=True, help="e.g. a90801ff (without checksum if --add-xor)")
    rp.add_argument("--add-xor", action="store_true", help="append XOR checksum byte")
    rp.add_argument("--listen", type=float, default=6.0)
    lv = sub.add_parser("live", help="poll telemetry like the app does (run while belt moving)")
    lv.add_argument("--time", type=float, default=60.0)
    lv.add_argument("--interval", type=float, default=2.0)
    hs = sub.add_parser("history", help="query workout history (distance/time)")
    hs.add_argument("--listen", type=float, default=8.0)
    sub.add_parser("status", help="live speed + last run distance/time")
    args = p.parse_args()
    asyncio.run(main_async(args))

if __name__ == "__main__":
    main()
