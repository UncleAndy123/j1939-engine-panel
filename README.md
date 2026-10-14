# J1939 Engine Panel

Dark, digital-gauge dashboard and TSC1 throttle control for Cummins engines over SAE J1939, using the
[Copperhill jCOM.J1939.USB gateway](https://copperhilltech.com/sae-j1939-starter-kit-and-network-simulator/)
(SAE J1939 Starter Kit). Built and tested against a **Cummins QSC8.3 / CM850 (CM2850)** on a test bench.

Single Python file, Tkinter UI. The only dependency is `pyserial`.

![Screenshot](docs/screenshot.png)

## Features

- **Live dashboard**
  - Engine RPM (big dial, with a marker at the requested speed while throttle is active).
  - Dials for oil pressure, coolant temp, boost and engine load, with warning and critical zones.
  - Tiles for oil, fuel and intake temps, fuel rate, fuel pressure, battery, torque, pedal, coolant level and ECM temp.
  - Engine hours and total fuel used, read on request.
- **Throttle (TSC1 engine speed control)**
  - Slider, presets and ±50 rpm buttons; arrow keys change the request by ±25.
  - Min/max RPM limits.
  - Live **Engine Torque Mode** readout, and an indicator showing whether the ECM is accepting or ignoring TSC1.
- **Faults (DM1)**: protect, amber, red stop and MIL lamps, water-in-fuel, and active codes (SPN, FMI, occurrence count) with descriptions.
- **Diagnostics log**
  - Every new PGN and source address seen on the bus.
  - Gateway version, heartbeat and serial error counters.
  - A 5-second bus health report after connecting.
- **DEMO mode**: a simulated gateway and engine, so the UI can be tried without hardware.

## Requirements

- Python 3.9+ with Tkinter (included in the standard Windows installer)
- `pyserial`
- Copperhill jCOM.J1939.USB board.
  - USB chip: SiLabs CP2102. Windows 10 installs the driver automatically; on Windows 11, get it from [silabs.com](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers).

## Install & run

```bash
pip install -r requirements.txt
python j1939_engine_panel.py
```

Behind an SSL-inspecting proxy:

```bash
pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

## Usage

1. Select the jCOM COM port (or **DEMO**). Baud **115200**, CAN **250** kbps (CM850/CM2850).
2. Click **CONNECT**. The gateway starts in listen-only mode and the dashboard fills in.
3. **Throttle:** with the engine running, set **My SA** (default `3`), **Engine SA** (`0`) and min/max RPM, then **ARM**.
   - The app claims the address and the gateway sends TSC1 every 10 ms.
   - Move the slider and watch the response line and the torque mode.
4. **READ HOURS & FUEL** claims an address and requests engine hours and total fuel used every 5 s.

If the ECM ignores TSC1, try another source address (for example 39 / 0x27, 17 / 0x11, or 230 / 0xE6). Some calibrations only accept TSC1 from specific addresses.

## ⚠️ Safety

- **While ARMED, the gateway repeats TSC1 on its own.** If the PC or app crashes, the engine **holds the last requested speed**.
- Kill switch: **STOP / Esc / Space**, **unplug the jCOM USB** (the board is USB powered), or **key off**.
- Built-in watchdogs drop the override:
  - if RPM data is lost for 1.5 s;
  - on overspeed (max RPM + 300).
- ARM is refused unless the engine is running (above 400 rpm).
- Test on a bench or with the machine in neutral and parked. Keep max RPM conservative.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `GATEWAY OK` but `BUS 0 msg/s` | Key off; CAN-H/CAN-L swapped; termination wrong (aim for ~60 Ω between CAN-H and CAN-L with power off) |
| No gateway ACKs | Old ACK setting stored in the gateway. Connect now forces ACKs back on |
| Wrong CAN speed | CM850/CM2850 = 250 kbps. Some newer engines use 500 kbps |
| ECM ignores TSC1 | TSC1 not allowed from that source address in the calibration. Try other SAs |

## How it works

- **Gateway link:** talks to the jCOM gateway over its documented serial protocol
  ([PDF](https://copperhilltech.com/content/jCOM1939-Protocol.pdf)).
  - Frames are byte-stuffed, start with `0xC0`, and carry a 2's-complement checksum.
  - Commands used: RESET, ADDFILTER, SETPARAM1 (address claim), TXDATA / TXDATAP (periodic transmit), REQINFO.
- **J1939 data:** decoded per SAE J1939-71.
  - Parameter groups: EEC1/2, ET1/2, EFL/P1, LFE, IC1, VEP1, HOURS, LFC, WFI.
  - Fault codes: DM1 (J1939-73).
