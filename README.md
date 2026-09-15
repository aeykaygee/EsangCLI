# EsangCLI
CLI app to control your Esang based under-desk treadmill

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Copy the example settings: `copy settings.example.toml settings.toml`
   (or `cp settings.example.toml settings.toml` on macOS/Linux)
3. Leave `address` empty in `settings.toml` and the apps will find the
   treadmill automatically on first run and save it back to the file.
   Never commit your real `settings.toml` — it contains your treadmill's
   Bluetooth address (it is git-ignored).

## Finding your treadmill's MAC address

Any one of these works. The address looks like `AA:BB:CC:DD:EE:FF`.

**Option 1 — auto-scan (easiest).** Leave `address = ""` and run
`python tui.py` or any `treadmill.py` command without `--address`.
The EsangLinker device is detected automatically.

**Option 2 — CLI scan.** With the treadmill powered on and the Gymax app
closed (it only allows one BLE connection), run:

```
python treadmill.py scan
```

Look for the row marked `LIKELY TREADMILL` (`EsangLinker` / `ESLinker` /
`EsangLinker`). Ignore the treadmill's Bluetooth *speaker* — that's a
separate Classic Bluetooth audio device and can't control the belt.

**Option 3 — nRF Connect (Android).** Install `nRF Connect for Mobile`,
scan, and connect to `EsangLinker`. Confirm it exposes service `0xFFF0`
with characteristics `FFF1` (notify) and `FFF2` (write) — that's the
control channel. The device MAC shown there is the address to put in
`settings.toml`.

## Usage

```
python tui.py                 # full-screen TUI: Right=start, Left=stop,
                              # Up/Down=speed, s=save default, q=quit
python tui.py --demo          # simulated treadmill, no hardware needed

python treadmill.py scan
python treadmill.py run --kmh 3.0
python treadmill.py speed --kmh 2.0
python treadmill.py stop
python treadmill.py status    # live speed + last run distance/time
```

## Calibration

`stride_m` in `settings.toml` converts distance to estimated steps.
Walk 100 steps, measure the meters, divide by 100, and store the result.
