# Changelog

All notable changes to J1939 Engine Panel are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Engine sound in DEMO mode:** a real-time synthesized inline-6 diesel sound that follows the displayed RPM and load. It has firing pulses at the true firing frequency, combustion clatter that grows with load, crank lope and a faint turbo whine.
  - Toggle it with the **SOUND** button in the header.
  - It needs the optional `sounddevice` package. The Windows exe includes it; without it, the app runs silently.

## [1.0.1] - 2026-10-22

### Changed
- Release notes moved into this changelog. `CHANGELOG-latest.md` holds only the notes for the next release.

## [1.0.0] - 2026-10-22

First public release. Windows users can download `J1939EnginePanel.exe` from the
[release page](https://github.com/UncleAndy123/j1939-engine-panel/releases/tag/v1.0.0).

### Added
- **Live dashboard** (dark, digital-gauge UI):
  - Engine RPM dial, with a marker at the requested speed while the throttle is armed.
  - Dials for oil pressure, coolant temperature, boost and engine load, with warning and critical zones.
  - Tiles for oil, fuel and intake temperatures, fuel rate, fuel pressure, battery voltage, torque, pedal position, coolant level and ECM temperature.
  - Engine hours and total fuel used, read on request with **READ HOURS & FUEL**.
- **TSC1 throttle control:**
  - Requested-speed slider, IDLE/1000/1400/1800 presets, ±50 rpm buttons, and ±25 rpm on the arrow keys.
  - Min/max RPM limits.
  - Live Engine Torque Mode readout, and an indicator showing whether the ECM is accepting or ignoring TSC1.
- **Safety:**
  - STOP button, also on Esc and Space.
  - ARM requires a running engine (above 400 rpm).
  - The throttle releases if RPM data is lost for 1.5 s, or on overspeed (max RPM + 300).
- **Faults (DM1):** protect, amber, red-stop and MIL lamps, water-in-fuel, and active codes (SPN, FMI, occurrence count) with descriptions.
- **Adapters:**
  - **Copperhill jCOM.J1939.USB** serial gateway.
  - **RP1210 adapters**, including the Cummins INLINE 6/7/8 and Nexiq. Installed adapters are detected from `RP121032.ini` and listed in the **PORT** menu. Simulator drivers and legacy numbered COM entries are hidden.
  - **RP1210 fail-safe:** the app repeats TSC1 itself, gated by a heartbeat from the UI. If the UI stalls for more than 0.5 s, TSC1 stops and stays off until you ARM again.
- **DEMO mode:** a simulated gateway and engine, for trying the app without hardware.
- **Diagnostics log:** every new PGN and source address seen, gateway status and error counters, and a 5-second bus health report after connecting.
- **Update check:** at startup the app checks GitHub for a newer release and highlights the version label when one exists. Click the label to check manually or to open the download page.
- App icon, and the version shown in the title bar and header.
- Windows exe built automatically by GitHub Actions: single file, 32-bit for RP1210 driver compatibility, with file version information.

### Fixed
These were found during development, before the release:
- **RP1210:** the address-claim command sent its bytes in the wrong order, so throttle control and hours/fuel requests failed on RP1210 adapters.
- **RP1210:** the gateway status no longer stays green when the adapter is unplugged. Read errors are logged without flooding the log.
- **RP1210:** disconnecting waits for any driver call in progress, and nothing is sent after disconnect.
- **jCOM:** Connect now turns gateway acknowledgements back on, in case an earlier tool switched them off.
- Readings are timestamped when they arrive, so dragging the window on Windows no longer trips a false "lost engine RPM data" stop.
- The PORT drop-down widens to show full adapter names.

[Unreleased]: https://github.com/UncleAndy123/j1939-engine-panel/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/UncleAndy123/j1939-engine-panel/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/UncleAndy123/j1939-engine-panel/releases/tag/v1.0.0
