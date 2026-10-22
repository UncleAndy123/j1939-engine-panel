## J1939 Engine Panel v1.0.0

First release. Dark digital dashboard and TSC1 throttle control for Cummins engines over SAE J1939.

### Download
**`J1939EnginePanel.exe`**: a single file with no install, and no Python needed. Windows 10/11.

> Windows may show a SmartScreen warning because the exe isn't code-signed. Click **More info → Run anyway**.

### Features
- **Live dashboard:** RPM, oil pressure, coolant, boost, load, temperatures, fuel rate, battery, coolant level, and ECM temperature. Engine hours and total fuel used are read on request.
- **Throttle:** TSC1 engine-speed control with min/max limits, presets, a live torque-mode readout, and an ECM accepting/ignoring indicator.
- **Faults:** DM1 lamps, water-in-fuel, and active SPN/FMI codes with descriptions.
- **Adapters:**
  - Copperhill jCOM.J1939.USB.
  - **RP1210 adapters**, including the Cummins INLINE 6/7/8, detected automatically from the installed drivers. Close INSITE before connecting.
- **DEMO mode:** try the app without hardware.
- **Update check:** the version label shows when a newer release is available. Click it to check manually.

### Safety
Test on a bench, or with the machine parked in neutral. Kill switch: **STOP / Esc / Space**, unplug the adapter, or key off. On RP1210 adapters, the app releases the throttle if its window stops responding.
