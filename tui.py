"""EsangLinker treadmill TUI (SuperFit / Gymax / GoPlus).

Controls:
  Right  start belt at default speed
  Left   stop belt
  Up     speed +step
  Down   speed -step
  s      save current target speed as default in settings.toml
  q      quit (auto-stops the belt if running)

Stats refresh every 2s. Duration/distance/steps are tracked locally from the
live speed; the treadmill itself only reports totals after a run (shown on
  stop). Steps are estimated as distance / stride_m from settings.toml.

Usage:
  python tui.py                      # connect to address in settings.toml
  python tui.py --demo               # simulated treadmill, no hardware needed
  python tui.py --settings other.toml
"""
import argparse
import asyncio
import sys
import time
import tomllib
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from treadmill import (
    NOTIFY_UUID,
    QUERY_NOWSPEED,
    START_FRAME,
    STOP_FRAME,
    WRITE_UUID,
    decode_history_payload,
    find_device,
    frame,
    kmh_to_raw,
    speed_frame,
    xor_checksum,
)

POLL_INTERVAL = 2.0
STRIDE_M = 0.75
MIN_SPEED_KMH = 0.5
MAX_SPEED_KMH = 6.0

HISTORY_QUERY = frame("a9fa0101")


def load_settings(path: Path) -> dict:
    cfg = {
        "address": "",
        "default_speed_kmh": 3.0,
        "speed_step_kmh": 0.5,
        "stride_m": STRIDE_M,
    }
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f).get("treadmill", {})
        cfg.update({k: v for k, v in data.items() if k in cfg})
    return cfg


def save_settings(path: Path, cfg: dict) -> None:
    path.write_text(
        "[treadmill]\n"
        "# BLE address of the EsangLinker module. Leave empty to auto-scan on startup\n"
        "# (found address is saved back to this file).\n"
        f'address = "{cfg["address"]}"\n'
        "\n"
        "# Speed the belt starts at when you press Right, in km/h.\n"
        f'default_speed_kmh = {cfg["default_speed_kmh"]}\n'
        "\n"
        "# How much Up/Down arrows change the speed per press, in km/h.\n"
        f'speed_step_kmh = {cfg["speed_step_kmh"]}\n'
        "\n"
        "# Step length used to estimate steps from distance, in meters.\n"
        "# Calibrate: walk 100 steps, measure the meters, divide by 100.\n"
        f'stride_m = {cfg["stride_m"]}\n'
    )


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class DemoClient:
    """Simulated treadmill: speaks the same A9 frames, no hardware needed."""

    def __init__(self):
        self.speed_raw = 0
        self.target_raw = 0
        self.running = False
        self.run_distance_m = 0.0
        self.run_time_s = 0.0
        self._cb = None
        self._task = None
        self._last = time.monotonic()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self._task:
            self._task.cancel()

    async def start_notify(self, uuid, cb):
        self._cb = cb
        self._task = asyncio.create_task(self._loop())

    async def stop_notify(self, uuid):
        if self._task:
            self._task.cancel()
            self._task = None

    def _send(self, hx: str):
        if self._cb:
            self._cb(None, bytearray.fromhex(hx))

    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                now = time.monotonic()
                dt = now - self._last
                self._last = now
                if self.running:
                    self.speed_raw = self.target_raw
                    self.run_time_s += dt
                    self.run_distance_m += self.speed_raw / 10 / 3.6 * dt
                self._send(frame(f"a9f401{self.speed_raw:02x}").hex())
        except asyncio.CancelledError:
            pass

    async def write_gatt_char(self, data_or_uuid, data=None, response=False):
        payload = data if data is not None else data_or_uuid
        hx = bytes(payload).hex()
        await asyncio.sleep(0.1)
        if hx == START_FRAME.hex():
            self.running = True
            self._send(frame("a9090101").hex())
        elif hx == STOP_FRAME.hex():
            self.running = False
            self.speed_raw = 0
            self._send(frame("a9090100").hex())
        elif hx.startswith("a90101"):
            self.target_raw = int(hx[6:8], 16)
            if self.running:
                self.speed_raw = self.target_raw
            self._send(frame(f"a9e001{self.target_raw:02x}").hex())
        elif hx == QUERY_NOWSPEED.hex():
            self._send(frame(f"a9f401{self.speed_raw:02x}").hex())
        elif hx == HISTORY_QUERY.hex():
            dist = int(self.run_distance_m)
            t = int(self.run_time_s) + 3
            payload = bytearray(16)
            payload[1] = 1
            payload[4:6] = dist.to_bytes(2, "little")
            payload[8:10] = t.to_bytes(2, "big")
            body = bytes.fromhex("a9af10") + bytes(payload)
            self._send(body.hex())  # history body...
            self._send(f"{xor_checksum(body):02x}")  # ...+ checksum split, like hardware


class TreadmillApp(App):
    CSS = """
    #status { height: 3; border: solid green; padding: 0 1; }
    #status.running { border: solid yellow; }
    .stat { border: solid blue; height: 5; padding: 0 1; }
    .stat_title { color: cyan; }
    .stat_value { color: white; }
    #events { height: 4; border: solid grey; padding: 0 1; }
    """

    BINDINGS = [
        Binding("right", "start", "Start"),
        Binding("left", "stop", "Stop"),
        Binding("up", "faster", "+Speed"),
        Binding("down", "slower", "-Speed"),
        Binding("s", "save_default", "Save default"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, settings_path: Path, demo: bool = False):
        super().__init__()
        self.settings_path = settings_path
        self.cfg = load_settings(settings_path)
        self.demo = demo
        self.client = None
        self.connected = False
        self.running = False
        self.target_raw = kmh_to_raw(self.cfg["default_speed_kmh"])
        self.live_raw = 0
        self.run_start = None
        self.run_distance_m = 0.0
        self._last_tick = None
        self._pending_history = None
        self._poll_task = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Connecting…", id="status")
        with Horizontal():
            yield Vertical(
                Static("Duration", classes="stat_title"),
                Static("--:--", id="duration", classes="stat_value"),
                classes="stat",
            )
            yield Vertical(
                Static("Speed (km/h)", classes="stat_title"),
                Static("--", id="speed", classes="stat_value"),
                classes="stat",
            )
            yield Vertical(
                Static("Distance", classes="stat_title"),
                Static("--", id="distance", classes="stat_value"),
                classes="stat",
            )
            yield Vertical(
                Static("Steps (est.)", classes="stat_title"),
                Static("--", id="steps", classes="stat_value"),
                classes="stat",
            )
        yield Static("", id="events")
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(0.5, self._tick)
        self.run_worker(self._connect_and_serve(), exclusive=True)

    def _event(self, msg: str) -> None:
        try:
            self.query_one("#events", Static).update(msg)
        except Exception:
            pass

    def _set_status(self, msg: str, running: bool = False) -> None:
        try:
            w = self.query_one("#status", Static)
            w.update(msg)
            w.set_class(running, "running")
        except Exception:
            pass

    async def _connect_and_serve(self) -> None:
        address = self.cfg["address"]
        try:
            if self.demo:
                self.client = DemoClient()
                await self.client.__aenter__()
            else:
                if not address:
                    self._set_status("Scanning for EsangLinker…")
                    address = await find_device(None)
                    if not address:
                        self._set_status("Not found. Check treadmill BT, then restart.")
                        return
                    self.cfg["address"] = address
                    save_settings(self.settings_path, self.cfg)
                from bleak import BleakClient

                self.client = BleakClient(address)
                await self.client.__aenter__()
            self.connected = True
            await self.client.start_notify(NOTIFY_UUID, self._on_notify)
            self._set_status(
                f"Ready — press → to start at {self.target_raw / 10:.1f} km/h"
            )
            self._poll_task = asyncio.create_task(self._poll_loop())
        except Exception as e:
            self._set_status(f"Connection failed: {e}")

    async def _poll_loop(self) -> None:
        try:
            while self.connected:
                await self._write(QUERY_NOWSPEED)
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _write(self, data: bytes) -> None:
        if self.demo:
            await self.client.write_gatt_char(WRITE_UUID, data, response=False)
        else:
            await self.client.write_gatt_char(WRITE_UUID, data, response=False)

    def _on_notify(self, _, data: bytearray) -> None:
        hx = bytes(data).hex()
        if hx.startswith("a90801"):
            return  # handshake spam
        if len(data) == 1 and self._pending_history:
            # long history frames arrive as body + lone checksum byte
            full = self._pending_history + hx
            self._pending_history = None
            body = bytes.fromhex(full)
            if xor_checksum(body[:-1]) == body[-1]:
                d = decode_history_payload(full[6:-2])
                if d:
                    self._event(
                        f"Treadmill totals: {d['distance_m']}m in "
                        f"{fmt_duration(d['time_s_adj'])}"
                    )
            return
        if hx.startswith("a9af10"):
            self._pending_history = hx  # checksum arrives next
            return
        if hx.startswith("a9f401") and len(data) >= 4:
            self.live_raw = int(hx[6:8], 16)
        elif hx.startswith("a90901") and len(data) >= 4:
            st = int(hx[6:8], 16)
            if st == 0:
                self.running = False

    def _tick(self) -> None:
        now = time.monotonic()
        if self.running and self.run_start is not None:
            dt = now - (self._last_tick or now)
            spd = (self.live_raw or self.target_raw) / 10
            self.run_distance_m += spd / 3.6 * dt
            dur = now - self.run_start
        else:
            dt = 0
            dur = getattr(self, "_last_duration", 0)
        self._last_tick = now
        try:
            self.query_one("#duration", Static).update(fmt_duration(dur))
            live = self.live_raw / 10 if self.live_raw else 0
            self.query_one("#speed", Static).update(
                f"{live:.1f}  (target {self.target_raw / 10:.1f})"
            )
            self.query_one("#distance", Static).update(
                f"{self.run_distance_m / 1000:.2f} km"
            )
            self.query_one("#steps", Static).update(
                f"{int(self.run_distance_m / self.cfg['stride_m'])}"
            )
        except Exception:
            pass

    def _clamp_raw(self, raw: int) -> int:
        lo, hi = kmh_to_raw(MIN_SPEED_KMH), kmh_to_raw(MAX_SPEED_KMH)
        return max(lo, min(hi, raw))

    async def _do_start(self) -> None:
        if not self.connected or self.running:
            return
        self.target_raw = self._clamp_raw(kmh_to_raw(self.cfg["default_speed_kmh"]))
        await self._write(START_FRAME)
        await asyncio.sleep(0.5 if self.demo else 3.0)
        await self._write(speed_frame(self.target_raw))
        self.running = True
        self.run_start = time.monotonic()
        self.run_distance_m = 0.0
        self._set_status(f"Running at {self.target_raw / 10:.1f} km/h", running=True)

    async def _do_stop(self) -> None:
        if not self.connected or not self.running:
            return
        await self._write(STOP_FRAME)
        self.running = False
        self._last_duration = time.monotonic() - (self.run_start or time.monotonic())
        self._set_status("Stopped — fetching totals…")
        await asyncio.sleep(0.5)
        await self._write(HISTORY_QUERY)
        await asyncio.sleep(0.5)
        self._set_status("Ready — press → to start")

    async def _do_speed(self, delta_raw: int) -> None:
        if not self.connected or not self.running:
            return
        self.target_raw = self._clamp_raw(self.target_raw + delta_raw)
        await self._write(speed_frame(self.target_raw))
        self._set_status(
            f"Running at {self.target_raw / 10:.1f} km/h", running=True
        )

    async def action_start(self) -> None:
        self.run_worker(self._do_start())

    async def action_stop(self) -> None:
        self.run_worker(self._do_stop())

    async def action_faster(self) -> None:
        self.run_worker(self._do_speed(kmh_to_raw(self.cfg["speed_step_kmh"])))

    async def action_slower(self) -> None:
        self.run_worker(self._do_speed(-kmh_to_raw(self.cfg["speed_step_kmh"])))

    async def action_save_default(self) -> None:
        self.cfg["default_speed_kmh"] = round(self.target_raw / 10, 1)
        save_settings(self.settings_path, self.cfg)
        self._event(f"Default speed saved: {self.cfg['default_speed_kmh']:.1f} km/h")

    async def action_quit(self) -> None:
        if self.running and self.client:
            try:  # never leave the belt running unattended
                await self._write(STOP_FRAME)
            except Exception:
                pass
        if self._poll_task:
            self._poll_task.cancel()
        if self.client and not self.demo:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception:
                pass
        self.exit()


def main() -> None:
    p = argparse.ArgumentParser(description="EsangLinker treadmill TUI")
    p.add_argument("--settings", default=str(Path(__file__).parent / "settings.toml"))
    p.add_argument("--demo", action="store_true", help="simulated treadmill")
    args = p.parse_args()
    TreadmillApp(Path(args.settings), demo=args.demo).run()


if __name__ == "__main__":
    main()
