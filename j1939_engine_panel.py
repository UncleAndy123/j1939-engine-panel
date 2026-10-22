#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
J1939 Engine Panel  -  dark digital dashboard + TSC1 throttle for Cummins engines over J1939.
Adapters: Copperhill jCOM.J1939.USB (serial) or any RP1210 adapter (Cummins INLINE, etc.).
Pick port "DEMO" to try it without hardware.

Requires:  pip install pyserial        (Tkinter ships with Python)

SAFETY: with the jCOM gateway, while ARMED the gateway repeats TSC1 every 10 ms on its own;
if this PC or app dies, the engine HOLDS the last speed. Kill: STOP / Esc / Space,
unplug the adapter, or key off.
"""
import json, math, queue, random, sys, threading, time, tkinter as tk, urllib.request, webbrowser
from tkinter import ttk, messagebox, font as tkfont

__version__ = "1.0.1"
APP_NAME = "J1939 Engine Panel"
GITHUB_REPO = "UncleAndy123/j1939-engine-panel"


def resource_path(rel):
    """Path to a bundled file, both when run from source and from a PyInstaller exe."""
    import os
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _vtuple(v):
    """'v1.2.10' -> (1, 2, 10); ignores any suffix like '-beta'."""
    parts = []
    for p in v.strip().lstrip("vV").split("-")[0].split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts + [0] * (3 - len(parts)))


def check_latest_release(timeout=8):
    """Return (tag, html_url, notes) of the latest GitHub release, or raise."""
    req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": f"{APP_NAME}/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # uses Windows proxy + cert store
        data = json.load(r)
    return data.get("tag_name", ""), data.get("html_url", ""), data.get("body", "") or ""

try:
    import serial, serial.tools.list_ports
except ImportError:
    serial = None

# =============================================================== jCOM protocol
START, ESC, START_STUFF, ESC_STUFF = 0xC0, 0xDB, 0xDC, 0xDD
ACK, ADDFILTER, TXDATA, RXDATA, RESET, HEART = 0, 1, 3, 4, 5, 6
REQINFO, REPSTATUS, SETACK, VERSION, SETPARAM1, TXDATAP, RXDATAX = 8, 9, 11, 13, 14, 17, 19


def frame(payload: bytes) -> bytes:
    n = len(payload) + 1
    body = bytes([n >> 8, n & 0xFF]) + payload
    chk = (-sum(body)) & 0xFF
    out = bytearray([START])
    for b in body + bytes([chk]):
        if b == START:
            out += bytes([ESC, START_STUFF])
        elif b == ESC:
            out += bytes([ESC, ESC_STUFF])
        else:
            out.append(b)
    return bytes(out)


class Parser:
    def __init__(self):
        self.buf, self.esc = None, False

    def feed(self, data):
        msgs = []
        for b in data:
            if b == START:
                self.buf, self.esc = bytearray(), False
                continue
            if self.buf is None:
                continue
            if self.esc:
                self.esc = False
                b = START if b == START_STUFF else ESC if b == ESC_STUFF else None
                if b is None:
                    self.buf = None
                    continue
            elif b == ESC:
                self.esc = True
                continue
            self.buf.append(b)
            if len(self.buf) >= 2 and len(self.buf) == ((self.buf[0] << 8) | self.buf[1]) + 2:
                if sum(self.buf) & 0xFF == 0:
                    msgs.append(bytes(self.buf[2:-1]))
                self.buf = None
        return msgs


def pgn3(pgn):
    return bytes([(pgn >> 16) & 0xFF, (pgn >> 8) & 0xFF, pgn & 0xFF])


class GatewayBase:
    def reset(self, kbps=250):
        self.send(bytes([RESET, 0xA5, 0x69, 0x5A if kbps == 250 else 0x5B]))

    def add_filter(self, pgn):
        self.send(bytes([ADDFILTER]) + pgn3(pgn))

    def claim(self, name8, sa):
        self.send(bytes([SETPARAM1]) + name8 + bytes([sa, 254, 254, 1]))

    def tx(self, pgn, da, sa, prio, data):
        self.send(bytes([TXDATA]) + pgn3(pgn) + bytes([da, sa, prio]) + bytes(data))

    def tx_periodic(self, pgn, da, sa, prio, ms, data):
        self.send(bytes([TXDATAP]) + pgn3(pgn) + bytes([da, sa, prio, ms >> 8, ms & 0xFF]) + bytes(data))

    def request(self, pgn, da, sa):
        self.tx(PGN_REQ, da, sa, 6, [pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF])


class Gateway(GatewayBase):
    def __init__(self, port, baud, q):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.q, self.lock, self.alive = q, threading.Lock(), True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        p = Parser()
        while self.alive:
            try:
                data = self.ser.read(1024)
            except Exception as e:
                if self.alive:
                    self.q.put(("err", str(e), time.time()))
                return
            now = time.time()
            for m in p.feed(data):
                self.q.put(("msg", m, now))

    def send(self, payload):
        with self.lock:
            self.ser.write(frame(payload))

    def close(self):
        self.alive = False
        try:
            self.ser.close()
        except Exception:
            pass


# =============================================================== J1939 decoding
PGN_TSC1, PGN_REQ, PGN_EEC2, PGN_EEC1 = 0x0000, 0xEA00, 0xF003, 0xF004
PGN_DM1, PGN_ET2, PGN_HOURS, PGN_LFC = 0xFECA, 0xFEA4, 0xFEE5, 0xFEE9
PGN_ET1, PGN_EFLP1, PGN_LFE, PGN_IC1, PGN_VEP1, PGN_WFI = 0xFEEE, 0xFEEF, 0xFEF2, 0xFEF6, 0xFEF7, 0xFEFF


def u8(d, i):
    return None if d[i] >= 0xFB else d[i]


def u16(d, i):
    v = d[i] | (d[i + 1] << 8)
    return None if v >= 0xFB00 else v


def u32(d, i):
    v = d[i] | (d[i + 1] << 8) | (d[i + 2] << 16) | (d[i + 3] << 24)
    return None if v >= 0xFB000000 else v


def sc(v, k, off=0):
    return None if v is None else v * k + off


C2F = lambda c: None if c is None else c * 9 / 5 + 32
KPA2PSI = lambda k: None if k is None else k * 0.145038
L2GAL = lambda l: None if l is None else l / 3.78541

DECODERS = {
    PGN_EEC1:  lambda d: {"rpm": sc(u16(d, 3), 0.125), "torque": sc(u8(d, 2), 1, -125),
                          "tqmode": None if (d[0] & 0x0F) == 0x0F else d[0] & 0x0F},
    PGN_EEC2:  lambda d: {"pedal": sc(u8(d, 1), 0.4), "load": u8(d, 2)},
    PGN_ET1:   lambda d: {"coolant": C2F(sc(u8(d, 0), 1, -40)), "fueltemp": C2F(sc(u8(d, 1), 1, -40)),
                          "oiltemp": C2F(sc(u16(d, 2), 0.03125, -273))},
    PGN_EFLP1: lambda d: {"fuelpress": KPA2PSI(sc(u8(d, 0), 4)), "oilpress": KPA2PSI(sc(u8(d, 3), 4)),
                          "coollvl": sc(u8(d, 7), 0.4)},
    PGN_LFE:   lambda d: {"fuelrate": L2GAL(sc(u16(d, 0), 0.05))},
    PGN_IC1:   lambda d: {"boost": KPA2PSI(sc(u8(d, 1), 2)), "intake": C2F(sc(u8(d, 2), 1, -40))},
    PGN_VEP1:  lambda d: {"battery": sc(u16(d, 4), 0.05)},
    PGN_ET2:   lambda d: {"ecmtemp": C2F(sc(u16(d, 2), 0.03125, -273))},
    PGN_HOURS: lambda d: {"hours": sc(u32(d, 0), 0.05)},
    PGN_LFC:   lambda d: {"fuelused": L2GAL(sc(u32(d, 4), 0.5))},
    PGN_WFI:   lambda d: {"wif": None if (d[0] & 3) == 3 else d[0] & 3},
}


def parse_dm1(d):
    b = d[0]
    lamps = {"PROTECT": b & 3, "AMBER": (b >> 2) & 3, "STOP": (b >> 4) & 3, "MIL": (b >> 6) & 3}
    dtcs = []
    for i in range(2, len(d) - 3, 4):
        spn = d[i] | (d[i + 1] << 8) | ((d[i + 2] >> 5) << 16)
        if spn in (0, 0x7FFFF):
            continue
        dtcs.append((spn, d[i + 2] & 0x1F, d[i + 3] & 0x7F))
    return lamps, dtcs


TORQUE_MODES = {0: "Low idle governor / no request", 1: "Accelerator pedal / operator", 2: "Cruise control",
                3: "PTO governor", 4: "Road speed governor", 5: "ASR control", 6: "Transmission control (TSC1)",
                7: "ABS control", 8: "Torque limiting", 9: "High speed governor", 10: "Braking system",
                11: "Remote accelerator", 12: "Service procedure", 13: "Not defined", 14: "Other (TSC1?)"}

SPN_NAMES = {84: "Vehicle speed", 91: "Accelerator pedal position", 94: "Fuel delivery pressure",
             97: "Water in fuel", 100: "Engine oil pressure", 102: "Intake manifold pressure",
             105: "Intake manifold temp", 108: "Barometric pressure", 110: "Coolant temperature",
             111: "Coolant level", 157: "Fuel rail pressure", 158: "Keyswitch battery voltage",
             168: "Battery voltage", 174: "Fuel temperature", 175: "Oil temperature", 190: "Engine speed",
             627: "ECM power supply", 629: "ECM", 639: "J1939 datalink", 651: "Injector cyl 1",
             652: "Injector cyl 2", 653: "Injector cyl 3", 654: "Injector cyl 4", 655: "Injector cyl 5",
             656: "Injector cyl 6", 723: "Camshaft speed sensor", 1347: "Fuel pump actuator",
             1569: "Engine derate", 2789: "Turbo inlet temp", 3509: "Sensor supply 1", 3510: "Sensor supply 2"}

FMI_NAMES = {0: "high - severe", 1: "low - severe", 2: "erratic / intermittent", 3: "voltage high / short high",
             4: "voltage low / short low", 5: "current low / open", 6: "current high / grounded",
             7: "mechanical response", 8: "abnormal frequency", 9: "abnormal update rate",
             10: "abnormal rate of change", 11: "root cause unknown", 12: "bad device", 13: "out of calibration",
             14: "special instructions", 15: "high - least severe", 16: "high - moderate",
             17: "low - least severe", 18: "low - moderate", 19: "network data error", 31: "condition exists"}


def tsc1(rpm=None):
    """TSC1 data bytes. rpm=None -> override disabled."""
    if rpm is None:
        return [0xF0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
    raw = int(rpm / 0.125)
    return [0x01 | (1 << 4) | 0xC0, raw & 0xFF, raw >> 8, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]


NAME = bytes([0x39, 0x19, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])  # LSB first, non-arbitrary


# =============================================================== RP1210 adapters (Cummins INLINE, Nexiq, etc.)
import configparser, ctypes, os, re, struct

# RP1210 command numbers
RP_SET_ALL_FILTERS_PASS = 3
RP_PROTECT_J1939_ADDRESS = 19


def rp1210_devices(windir=None):
    """Read RP121032.ini and each vendor .ini; return {label: (dll_name, device_id)}."""
    windir = windir or os.environ.get("WINDIR", r"C:\Windows")
    out = {}

    def read_ini(path):
        cp = configparser.ConfigParser(strict=False, interpolation=None, inline_comment_prefixes=(";",))
        cp.optionxform = str
        try:
            with open(path, encoding="latin-1") as f:
                cp.read_file(f)
            return cp
        except (OSError, configparser.Error):
            return None                                 # unreadable or malformed vendor ini -> skip it

    main = read_ini(os.path.join(windir, "RP121032.ini"))
    if not main or not main.has_option("RP1210Support", "APIImplementations"):
        return out
    for impl in [s.strip() for s in main.get("RP1210Support", "APIImplementations").split(",") if s.strip()]:
        cp = read_ini(os.path.join(windir, impl + ".ini"))
        if not cp:
            continue
        vendor = cp.get("VendorInformation", "Name", fallback=impl).strip()
        if "mock" in vendor.lower() or "nemesis" in vendor.lower():
            continue                                    # skip simulator driver
        for sec in cp.sections():
            if not sec.startswith("DeviceInformation"):
                continue
            try:
                dev = int(cp.get(sec, "DeviceID"))
            except (ValueError, configparser.Error):
                continue
            desc = cp.get(sec, "DeviceDescription", fallback=sec).strip()
            if re.search(r",\s*COM\d+$", desc):          # drop legacy numbered serial ports (keep USB / Auto Detect)
                continue
            out[f"RP1210 · {desc} [{impl}:{dev}]"] = (impl, dev)
    return out


def _load_rp1210_dll(name):
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for p in (os.path.join(windir, name + ".dll"),
              os.path.join(windir, "SysWOW64", name + ".dll"),
              os.path.join(windir, "System32", name + ".dll"),
              name):
        try:
            return ctypes.WinDLL(p)
        except OSError as e:
            last = e
    bits = struct.calcsize("P") * 8
    hint = "  This driver is 32-bit — run the app with 32-bit Python." if bits == 64 else ""
    raise OSError(f"Could not load {name}.dll: {last}.{hint}")


class RP1210Gateway:
    """Drives an RP1210 adapter (Cummins INLINE, Nexiq, etc.) but presents the same
    message interface as Gateway/DemoGateway, so the rest of the app is unchanged.

    RP1210 has no 'periodic transmit' feature, so TSC1 is repeated from a thread here.
    The GUI must call kick() regularly; if it stops for >0.5 s the periodic TSC1 stops,
    so a frozen or crashed UI releases the throttle instead of holding the last speed.
    """

    def __init__(self, dll_name, device_id, kbps, q):
        self.q, self.alive, self.lock = q, True, threading.Lock()
        self.sa = None
        self.periodic = {}                               # pgn -> [interval_s, next_t, da, sa, prio, data]
        self.last_kick = time.time()
        self.link_ok = True                              # False while ReadMessage is returning errors
        self._last_log = {}                              # message -> time, to rate-limit repeated errors
        self.dll = _load_rp1210_dll(dll_name)
        d = self.dll
        d.RP1210_ClientConnect.restype = ctypes.c_short
        d.RP1210_ClientConnect.argtypes = [ctypes.c_void_p, ctypes.c_short, ctypes.c_char_p,
                                           ctypes.c_long, ctypes.c_long, ctypes.c_short]
        for fn, res, args in (
            ("RP1210_ClientDisconnect", ctypes.c_short, [ctypes.c_short]),
            ("RP1210_SendMessage", ctypes.c_short,
             [ctypes.c_short, ctypes.c_char_p, ctypes.c_short, ctypes.c_short, ctypes.c_short]),
            ("RP1210_ReadMessage", ctypes.c_short,
             [ctypes.c_short, ctypes.c_char_p, ctypes.c_short, ctypes.c_short]),
            ("RP1210_SendCommand", ctypes.c_short,
             [ctypes.c_short, ctypes.c_short, ctypes.c_char_p, ctypes.c_short]),
            ("RP1210_GetErrorMsg", ctypes.c_short, [ctypes.c_short, ctypes.c_char_p]),
        ):
            try:
                getattr(d, fn).restype = res
                getattr(d, fn).argtypes = args
            except AttributeError:
                pass

        self.name = f"{dll_name}:{device_id}"
        cid = d.RP1210_ClientConnect(None, device_id, f"J1939:Baud={kbps}".encode(), 0, 0, 0)
        if cid < 0 or cid > 127:                         # 0..127 = client id, else error code
            raise OSError(f"ClientConnect failed: {self._err(cid)}")
        self.cid = cid

        self._winmm = None
        try:
            self._winmm = ctypes.WinDLL("winmm")
            self._winmm.timeBeginPeriod(1)               # 1 ms timer resolution for the 10 ms TSC1 loop
        except Exception:
            self._winmm = None

        self._log(f"RP1210 {self.name} connected (client {cid}, J1939 @ {kbps}k)")
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._periodic_tx, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    # ---- helpers
    def _err(self, code):
        buf = ctypes.create_string_buffer(256)
        try:
            self.dll.RP1210_GetErrorMsg(abs(int(code)), buf)
            txt = buf.value.decode(errors="ignore").strip()
        except Exception:
            txt = ""
        return f"error {abs(int(code))}" + (f" ({txt})" if txt else "")

    def _put(self, payload):
        self.q.put(("msg", bytes(payload), time.time()))

    def _log(self, s, every=0.0):
        now = time.time()
        if every and now - self._last_log.get(s, 0) < every:
            return
        self._last_log[s] = now
        self.q.put(("log", s, now))

    def _driver_version(self):
        try:
            bufs = [ctypes.create_string_buffer(32) for _ in range(4)]
            with self.lock:
                self.dll.RP1210_ReadVersion(*bufs)
            v = [b.value.decode(errors="ignore").strip() for b in bufs]
            return f"DLL {v[0]}.{v[1]}, RP1210 API {v[2]}.{v[3]}"
        except Exception:
            return "version unavailable"

    # ---- control messages from the app (same bytes the jCOM gateway would get)
    def send(self, payload):
        mid = payload[0]
        if mid == REQINFO and len(payload) >= 2:
            if payload[1] == VERSION:
                self._log(f"RP1210 driver {self.name}: {self._driver_version()}")
            elif payload[1] == REPSTATUS:
                self._put([REPSTATUS, 2 if self.sa is not None else 4,
                           self.sa if self.sa is not None else 254])
        # SETACK and anything else: nothing to do (RP1210 has no equivalent)

    def reset(self, kbps=250):
        pass                                             # baud is fixed at ClientConnect

    def add_filter(self, pgn):
        with self.lock:
            self.dll.RP1210_SendCommand(RP_SET_ALL_FILTERS_PASS, self.cid, b"", 0)

    def claim(self, name8, sa):
        # Protect_J1939_Address block: address(1), NAME(8, LSB first), 0 = block until claim done.
        # The claim takes a few hundred ms, so run it off the UI thread; the result comes back as REPSTATUS.
        data = bytes([sa & 0xFF]) + bytes(name8[:8]).ljust(8, b"\x00") + bytes([0])

        def work():
            with self.lock:
                if not self.alive:
                    return
                r = self.dll.RP1210_SendCommand(RP_PROTECT_J1939_ADDRESS, self.cid, data, len(data))
            if r == 0:
                self.sa = sa
                self._put([REPSTATUS, 2, sa])
            else:
                self._log(f"RP1210 address claim failed: {self._err(r)}")
                self._put([REPSTATUS, 3, 254])
        threading.Thread(target=work, daemon=True).start()

    def tx(self, pgn, da, sa, prio, data):
        # RP1210 J1939 send block: PGN(3 LE), how/priority, source, dest, data
        msg = bytes([pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF, prio & 7, sa & 0xFF, da & 0xFF]) + bytes(data)
        with self.lock:
            if not self.alive:
                return
            r = self.dll.RP1210_SendMessage(self.cid, msg, len(msg), 0, 1)
        if r != 0:
            self._log(f"RP1210 send PGN 0x{pgn:05X} failed: {self._err(r)}", every=2.0)

    def tx_periodic(self, pgn, da, sa, prio, ms, data):
        if ms == 0:
            self.periodic.pop(pgn, None)
        else:
            self.periodic[pgn] = [ms / 1000.0, 0.0, da, sa, prio, bytes(data)]

    def request(self, pgn, da, sa):
        self.tx(PGN_REQ, da, sa, 6, [pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF])

    def kick(self):
        self.last_kick = time.time()

    # ---- threads
    def _reader(self):
        buf = ctypes.create_string_buffer(2048)
        while self.alive:
            with self.lock:
                if not self.alive:
                    return
                try:
                    n = self.dll.RP1210_ReadMessage(self.cid, buf, len(buf), 0)
                except Exception as e:
                    self.q.put(("err", str(e), time.time()))
                    return
            if 0 < n <= len(buf):
                self.link_ok = True
                if n >= 10:                              # timestamp(4) + PGN(3) + how(1) + src(1) + dst(1)
                    raw = buf.raw[:n]
                    pgn = raw[4] | (raw[5] << 8) | (raw[6] << 16)
                    src, dst, pdata = raw[8], raw[9], raw[10:]
                    self._put(bytes([RXDATA, (pgn >> 16) & 0xFF, (pgn >> 8) & 0xFF, pgn & 0xFF,
                                     dst, src, raw[7] & 7]) + pdata)
            elif n < 0:                                  # driver error (e.g. adapter unplugged)
                self.link_ok = False
                self._log(f"RP1210 read error: {self._err(n)}", every=5.0)
                time.sleep(0.05)
            else:
                time.sleep(0.001)                        # 0 = no message waiting

    def _periodic_tx(self):
        while self.alive:
            now = time.time()
            if self.periodic and now - self.last_kick >= 0.5:
                # Fail-safe: the UI stopped responding. Drop the override and latch it off;
                # the app must be re-armed, so the engine never jumps back on its own.
                self.periodic.clear()
                self.q.put(("disarm", "UI stalled - TSC1 released by fail-safe", now))
            for pgn, p in list(self.periodic.items()):
                if now >= p[1]:
                    p[1] = now + p[0]
                    self.tx(pgn, p[2], p[3], p[4], p[5])
            time.sleep(0.002)

    def _heartbeat(self):
        while self.alive:
            if self.link_ok:
                self._put([HEART])                       # adapter alive; no jCOM-style version fields
            time.sleep(1.0)

    def close(self):
        self.periodic.clear()
        with self.lock:                                  # wait for any in-flight driver call
            self.alive = False
            try:
                self.dll.RP1210_ClientDisconnect(self.cid)
            except Exception:
                pass
        if self._winmm:
            try:
                self._winmm.timeEndPeriod(1)
            except Exception:
                pass


# =============================================================== demo engine
class DemoGateway(GatewayBase):
    """Simulated gateway + engine so the UI can be tested without hardware."""

    def __init__(self, q):
        self.q, self.alive = q, True
        self.sa, self.tsc, self.rpm, self.t0 = None, None, 0.0, time.time()
        threading.Thread(target=self._run, daemon=True).start()

    def _put(self, payload):
        self.q.put(("msg", bytes(payload), time.time()))

    def _rx(self, pgn, data, sa=0, da=255, prio=6):
        self._put(bytes([RXDATA]) + pgn3(pgn) + bytes([da, sa, prio]) + bytes(data))

    def send(self, p):
        mid = p[0]
        if mid != REQINFO:
            self._put([ACK, mid])
        if mid == REQINFO:
            if p[1] == VERSION:
                self._put([VERSION, 6, 0, 0, 1, 33, 2])
            elif p[1] == REPSTATUS:
                self._put([REPSTATUS, 2 if self.sa is not None else 4, 254 if self.sa is None else self.sa])
        elif mid == RESET:
            self.sa, self.tsc = None, None
        elif mid == SETPARAM1:
            sa = p[9]

            def done():
                self.sa = sa
                self._put([REPSTATUS, 2, sa])
            threading.Timer(0.3, done).start()
        elif mid in (TXDATA, TXDATAP):
            pgn = (p[1] << 16) | (p[2] << 8) | p[3]
            data = p[9:] if mid == TXDATAP else p[7:]
            if pgn == PGN_TSC1:
                off = (mid == TXDATAP and ((p[7] << 8) | p[8]) == 0) or (data[0] & 3) == 0
                self.tsc = None if off else ((data[1] | (data[2] << 8)) * 0.125)
            elif pgn == PGN_REQ:
                req = data[0] | (data[1] << 8) | (data[2] << 16)
                if req == PGN_HOURS:
                    self._rx(PGN_HOURS, le32(int(1873.4 / 0.05)) + [0xFF] * 4)
                elif req == PGN_LFC:
                    self._rx(PGN_LFC, [0xFF] * 4 + le32(int(41250 / 0.5)))

    def close(self):
        self.alive = False

    def _run(self):
        last, sent = {}, 0

        def due(name, period):
            if now - last.get(name, 0) >= period:
                last[name] = now
                return True
            return False

        while self.alive:
            now = time.time()
            el = now - self.t0
            if el > 2 and self.rpm < 300:
                self.rpm = 550.0                                 # "starts"
            target = 0 if el < 2 else (self.tsc or 750)
            self.rpm += (target - self.rpm) * 0.025 + random.uniform(-2, 2) * (self.rpm > 300)
            run = self.rpm > 300
            fr = max(0.0, min(1.0, (self.rpm - 750) / 1350))
            load = (12 + 30 * fr + min(25, abs(target - self.rpm) / 30) + random.uniform(-1, 1)) if run else 0
            warm = 1 - math.exp(-el / 90)
            cool, oilt = 45 + 40 * warm, 40 + 50 * (1 - math.exp(-el / 150))
            oil = (190 + 230 * fr + random.uniform(-4, 4)) if run else 0
            boost = max(0.0, 150 * fr * load / 40) if run else 0
            rate = (1.6 + 34 * fr * load / 40) if run else 0
            r = int(self.rpm / 0.125)
            mode = 6 if self.tsc else 0
            if due("eec1", 0.02):
                self._rx(PGN_EEC1, [0xF0 | mode, b8(125 + load), b8(125 + load), r & 0xFF, r >> 8, 0xFF, 0xFF, 0xFF], prio=3)
            if due("eec2", 0.05):
                self._rx(PGN_EEC2, [0xFF, 0, b8(load), 0xFF, 0xFF, 0xFF, 0xFF, 0xFF], prio=3)
            if due("lfe", 0.1):
                self._rx(PGN_LFE, le16(int(rate / 0.05)) + [0xFF] * 6)
            if due("efl", 0.5):
                self._rx(PGN_EFLP1, [b8(550 / 4 if run else 0), 0xFF, 0xFF, b8(oil / 4), 0xFF, 0xFF, 50, 250])
            if due("ic1", 0.5):
                self._rx(PGN_IC1, [0xFF, b8(boost / 2), b8(35 + 10 * fr + 40), 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
            if due("slow", 1.0):
                self._rx(PGN_ET1, [b8(cool + 40), b8(34 + 6 * warm + 40)] + le16(int((oilt + 273) / 0.03125)) + [0xFF] * 4)
                self._rx(PGN_VEP1, [0xFF] * 4 + le16(int((27.7 if run else 24.3) / 0.05)) + [0xFF, 0xFF])
                self._rx(PGN_ET2, [0xFF, 0xFF] + le16(int((38 + 6 * warm + 273) / 0.03125)) + [0xFF] * 4)
                self._rx(PGN_DM1, [0x04, 0xFF, 0x73, 0x02, 0x02, 0x0E, 0xFF, 0xFF])
                self._rx(PGN_WFI, [0xFC, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
                self._put([HEART, 6, 0, 0, 1, 33, 2, 0, 0])
            time.sleep(0.01)


def b8(x):
    return max(0, min(250, int(x)))


def le16(v):
    return [v & 0xFF, (v >> 8) & 0xFF]


def le32(v):
    return [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]


# =============================================================== theme
BG, PANEL, PANEL2, BORDER = "#0a0e13", "#111821", "#17202b", "#223042"
TEXT, MUTED, DIM = "#e6edf3", "#7d8b9c", "#3a4656"
ACCENT, OK, WARN, CRIT, TRACK = "#22d3ee", "#34d399", "#fbbf24", "#f43f5e", "#1c2632"
F = {}          # fonts, filled at startup
K = 1.0         # UI scale (DPI)


def px(v):
    return int(round(v * K))


def blend(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))


def alarm_color(v, warn, crit, lwarn, lcrit, low_active):
    if crit is not None and v >= crit:
        return CRIT
    if warn is not None and v >= warn:
        return WARN
    if low_active and lcrit is not None and v <= lcrit:
        return CRIT
    if low_active and lwarn is not None and v <= lwarn:
        return WARN
    return ACCENT


def rrect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class ArcGauge(tk.Canvas):
    SWEEP, START = 240, 210

    def __init__(self, master, title, unit, lo, hi, bs, fmt="{:.0f}", warn=None, crit=None,
                 lwarn=None, lcrit=None, majors=5, labels=True, low_needs_run=True):
        s = px(bs)
        super().__init__(master, width=s, height=int(s * (0.9 if labels else 0.86)), bg=PANEL, highlightthickness=0)
        self.lo, self.hi, self.fmt, self.low_needs_run = lo, hi, fmt, low_needs_run
        self.th = (warn, crit, lwarn, lcrit)
        cx, tw = s / 2, max(5, s * 0.055)
        cy, r = (s * 0.53, s * 0.33) if labels else (s * 0.50, s * 0.385)   # labelled: scale outside arc
        self.cx, self.cy, self.r, self.tw = cx, cy, r, tw
        bb = (cx - r, cy - r, cx + r, cy + r)
        self.create_arc(*bb, start=self.START, extent=-self.SWEEP, style="arc", width=tw, outline=TRACK)
        zr = r + tw * 0.95
        zb = (cx - zr, cy - zr, cx + zr, cy + zr)

        def zone(a, b, col):
            fa, fb = self.frac(a), self.frac(b)
            if fb > fa:
                self.create_arc(*zb, start=self.START - self.SWEEP * fa, extent=-self.SWEEP * (fb - fa),
                                style="arc", width=max(2, tw * 0.28), outline=col)
        if warn is not None:
            zone(warn, crit if crit is not None else hi, blend(WARN, PANEL, 0.25))
        if crit is not None:
            zone(crit, hi, blend(CRIT, PANEL, 0.15))
        if lcrit is not None:
            zone(lo, lcrit, blend(CRIT, PANEL, 0.15))
        if lwarn is not None:
            zone(lcrit if lcrit is not None else lo, lwarn, blend(WARN, PANEL, 0.25))
        n = majors * 4
        for i in range(n + 1):
            t = i / n
            a = math.radians(self.START - self.SWEEP * t)
            major = i % 4 == 0
            r1 = r - tw * 0.95
            r2 = r1 - (tw * 0.8 if major else tw * 0.4)
            self.create_line(cx + r1 * math.cos(a), cy - r1 * math.sin(a), cx + r2 * math.cos(a),
                             cy - r2 * math.sin(a), fill=MUTED if major else DIM, width=2 if major else 1)
            if major and (labels or i in (0, n)):
                rl = r + tw * 2.3 if labels else r2 - tw * 1.3
                v = lo + (hi - lo) * t
                self.create_text(cx + rl * math.cos(a), cy - rl * math.sin(a), text=f"{v:g}",
                                 fill=MUTED, font=(F["ui"], max(7, int(bs * 0.03))))
        self.glow = self.create_arc(*bb, start=self.START, extent=-1, style="arc", width=tw * 2.2,
                                    outline=PANEL, state="hidden")
        self.val = self.create_arc(*bb, start=self.START, extent=-1, style="arc", width=tw,
                                   outline=ACCENT, state="hidden")
        self.marker = self.create_line(0, 0, 0, 0, fill=TEXT, width=3, state="hidden")
        self.txt = self.create_text(cx, cy - s * 0.01, text="--", fill=DIM,
                                    font=(F["dig"], max(14, int(bs * 0.14))))
        self.create_text(cx, cy + s * 0.12, text=unit, fill=MUTED, font=(F["ui"], max(8, int(bs * 0.04))))
        self.create_text(cx, cy + r * 0.78, text=title.upper(), fill=MUTED,
                         font=(F["ui"], max(8, int(bs * 0.036)), "bold"))

    def frac(self, v):
        return max(0.0, min(1.0, (v - self.lo) / (self.hi - self.lo)))

    def set(self, v, running=True, marker=None):
        if v is None:
            self.itemconfig(self.txt, text="--", fill=DIM)
            for i in (self.val, self.glow, self.marker):
                self.itemconfig(i, state="hidden")
            return
        col = alarm_color(v, *self.th, running or not self.low_needs_run)
        ext = -max(0.5, self.SWEEP * self.frac(v))
        self.itemconfig(self.val, extent=ext, outline=col, state="normal")
        self.itemconfig(self.glow, extent=ext, outline=blend(PANEL, col, 0.18), state="normal")
        self.itemconfig(self.txt, text=self.fmt.format(v), fill=TEXT if col == ACCENT else col)
        if marker is None:
            self.itemconfig(self.marker, state="hidden")
        else:
            a = math.radians(self.START - self.SWEEP * self.frac(marker))
            r1, r2 = self.r - self.tw * 1.1, self.r + self.tw * 1.4
            self.coords(self.marker, self.cx + r1 * math.cos(a), self.cy - r1 * math.sin(a),
                        self.cx + r2 * math.cos(a), self.cy - r2 * math.sin(a))
            self.itemconfig(self.marker, state="normal")


class Tile(tk.Canvas):
    def __init__(self, master, title, unit, bw, bh, fmt="{:.0f}", lo=None, hi=None, warn=None, crit=None,
                 lwarn=None, lcrit=None, low_needs_run=True):
        w, h = px(bw), px(bh)
        super().__init__(master, width=w, height=h, bg=PANEL, highlightthickness=0)
        self.fmt, self.lo, self.hi, self.low_needs_run = fmt, lo, hi, low_needs_run
        self.th = (warn, crit, lwarn, lcrit)
        self.w, self.h, pad = w, h, px(10)
        rrect(self, 1, 1, w - 1, h - 1, px(10), fill=PANEL2, outline=BORDER)
        self.create_text(pad, pad, text=title.upper(), anchor="nw", fill=MUTED, font=(F["ui"], 8, "bold"))
        self.txt = self.create_text(pad, h - px(13), text="--", anchor="sw", fill=DIM, font=(F["dig"], 20))
        self.create_text(w - pad, h - px(15), text=unit, anchor="se", fill=MUTED, font=(F["ui"], 9))
        self.bar = None
        if lo is not None:
            y = h - px(7)
            self.create_line(pad, y, w - pad, y, fill=TRACK, width=px(3), capstyle="round")
            self.bar = self.create_line(pad, y, pad, y, fill=ACCENT, width=px(3), capstyle="round", state="hidden")

    def set(self, v, running=True):
        if v is None:
            self.itemconfig(self.txt, text="--", fill=DIM)
            if self.bar:
                self.itemconfig(self.bar, state="hidden")
            return
        col = alarm_color(v, *self.th, running or not self.low_needs_run)
        self.itemconfig(self.txt, text=self.fmt.format(v), fill=TEXT if col == ACCENT else col)
        if self.bar:
            f = max(0.0, min(1.0, (v - self.lo) / (self.hi - self.lo)))
            pad, y = px(10), self.h - px(7)
            self.coords(self.bar, pad, y, pad + max(1, (self.w - 2 * pad) * f), y)
            self.itemconfig(self.bar, fill=col, state="normal")


class Lamp(tk.Canvas):
    def __init__(self, master, label, color):
        w, h = px(62), px(58)
        super().__init__(master, width=w, height=h, bg=PANEL, highlightthickness=0)
        self.color, cx, r = color, w / 2, px(11)
        self.halo = self.create_oval(cx - r * 1.7, px(18) - r * 1.7, cx + r * 1.7, px(18) + r * 1.7,
                                     fill=PANEL, outline="")
        self.dot = self.create_oval(cx - r, px(18) - r, cx + r, px(18) + r, fill=TRACK, outline=BORDER)
        self.create_text(cx, h - px(8), text=label, fill=MUTED, font=(F["ui"], 7, "bold"))

    def set(self, state):
        if state == 1:
            self.itemconfig(self.dot, fill=self.color, outline=self.color)
            self.itemconfig(self.halo, fill=blend(PANEL, self.color, 0.22))
        elif state == 2:      # error / unknown value
            self.itemconfig(self.dot, fill=TRACK, outline=WARN)
            self.itemconfig(self.halo, fill=PANEL)
        else:
            self.itemconfig(self.dot, fill=TRACK if state == 0 else PANEL, outline=BORDER)
            self.itemconfig(self.halo, fill=PANEL)


def mkbtn(parent, text, cmd, bg=PANEL2, fg=TEXT, font=None, **kw):
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=blend(bg, "#ffffff", 0.12),
                  activeforeground=fg, relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  font=font or (F["ui"], 9, "bold"), padx=px(12), pady=px(6), **kw)
    b._bg = bg
    b.bind("<Enter>", lambda e: b.config(bg=blend(b._bg, "#ffffff", 0.08)))
    b.bind("<Leave>", lambda e: b.config(bg=b._bg))

    def recolor(bg_, fg_=None):
        b._bg = bg_
        b.config(bg=bg_, activebackground=blend(bg_, "#ffffff", 0.12))
        if fg_:
            b.config(fg=fg_, activeforeground=fg_)
    b.recolor = recolor
    return b


def mkentry(parent, val, w=5):
    e = tk.Entry(parent, width=w, bg=PANEL2, fg=TEXT, insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                 font=(F["dig"], 11), justify="center")
    e.insert(0, val)
    return e


def pick(fams, *names):
    for n in names:
        if n in fams:
            return n
    return "TkDefaultFont"


# =============================================================== app
class App(tk.Tk):
    def __init__(self):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        super().__init__()
        global K
        K = max(1.0, float(self.tk.call("tk", "scaling")) / 1.3333)
        fams = set(tkfont.families(self))
        F.update(dig=pick(fams, "Bahnschrift SemiBold", "Bahnschrift", "DejaVu Sans Mono", "Consolas"),
                 ui=pick(fams, "Segoe UI", "Bahnschrift", "DejaVu Sans", "Helvetica"),
                 mono=pick(fams, "Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier"))
        self.title(f"{APP_NAME}  v{__version__}")
        try:
            if sys.platform == "win32":
                self.iconbitmap(default=resource_path("assets/icon.ico"))
            else:
                self._icon_img = tk.PhotoImage(file=resource_path("assets/icon-256.png"))
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass
        self.update_url = None
        self.configure(bg=BG)
        self.q, self.gw = queue.Queue(), None
        self.vals, self.stamp, self.dm1 = {}, {}, {}
        self.claimed, self.sa, self.armed, self.pending_arm, self.pending_read = False, None, False, False, False
        self.last_heart, self.heart_errs, self.last_req = 0, None, 0
        self.rx_count, self.ack_count, self.seen = 0, 0, set()
        self.rate_n, self.rate_t, self.rate = 0, time.time(), 0
        self.arm_time, self.base_mode, self.arm_rpm = 0, None, 0
        self._upd_pending, self._dtc_text = False, None
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_all("<Escape>", lambda e: self.stop())
        self.bind_all("<space>", self._space)
        for key, d in (("<Up>", 25), ("<Right>", 25), ("<Down>", -25), ("<Left>", -25)):
            self.bind_all(key, lambda e, d=d: self._nudge_key(e, d))
        self.after(20, self.poll)
        self.after(2000, self.check_updates)              # silent check for a newer GitHub release
        self.after(100, self.refresh)

    # ------------------------------------------------------------ layout
    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=TEXT,
                     arrowcolor=MUTED, bordercolor=BORDER, lightcolor=PANEL2, darkcolor=PANEL2,
                     selectbackground=PANEL2, selectforeground=TEXT, padding=px(4))
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", TEXT)],
               background=[("active", BORDER)], bordercolor=[("focus", ACCENT)])
        st.configure("Rpm.Horizontal.TScale", background=ACCENT, troughcolor=TRACK, bordercolor=PANEL,
                     lightcolor=ACCENT, darkcolor=ACCENT, gripcount=0, sliderlength=px(34),
                     sliderthickness=px(22), troughrelief="flat")
        st.map("Rpm.Horizontal.TScale", background=[("active", "#67e8f9")])
        self.option_add("*TCombobox*Listbox.background", PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", BG)

    def panel(self, parent, title):
        outer = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        hdr = tk.Frame(outer, bg=PANEL)
        hdr.pack(fill="x", padx=px(14), pady=(px(10), px(4)))
        tk.Label(hdr, text=title, bg=PANEL, fg=MUTED, font=(F["ui"], 9, "bold")).pack(side="left")
        inner = tk.Frame(outer, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=px(12), pady=(0, px(12)))
        return outer, hdr, inner

    def _lbl(self, parent, text, size=8, fg=MUTED, bg=PANEL, bold=True):
        return tk.Label(parent, text=text, bg=bg, fg=fg, font=(F["ui"], size, "bold" if bold else "normal"))

    def _build(self):
        # ---- header
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=px(16), pady=(px(12), px(8)))
        ttl = tk.Frame(hdr, bg=BG)
        ttl.pack(side="left")
        tk.Label(ttl, text="QSC8.3  ·  CM2850", bg=BG, fg=TEXT, font=(F["dig"], 18)).pack(anchor="w")
        pills = tk.Frame(ttl, bg=BG)
        pills.pack(anchor="w", pady=(px(2), 0))
        self.p_gw = self._lbl(pills, "● GATEWAY  OFFLINE", bg=BG, fg=DIM)
        self.p_bus = self._lbl(pills, "● BUS  --", bg=BG, fg=DIM)
        self.p_mode = self._lbl(pills, "● MODE  --", bg=BG, fg=DIM)
        self.p_ver = self._lbl(pills, f"v{__version__}", bg=BG, fg=DIM)
        self.p_ver.config(cursor="hand2")
        self.p_ver.bind("<Button-1>", lambda e: self.on_version_click())
        for p in (self.p_gw, self.p_bus, self.p_mode, self.p_ver):
            p.pack(side="left", padx=(0, px(16)))

        ctl = tk.Frame(hdr, bg=BG)
        ctl.pack(side="right")
        self._lbl(ctl, "PORT", bg=BG).pack(side="left", padx=(0, px(4)))
        self.port = ttk.Combobox(ctl, width=32, values=self.ports(), font=(F["ui"], 10),
                                 style="Port.TCombobox", postcommand=lambda: self._widen_dropdown(self.port))
        self.port.pack(side="left")
        if self.port["values"]:
            self.port.current(0)
        mkbtn(ctl, "⟳", lambda: self.port.config(values=self.ports()), font=(F["ui"], 10)).pack(side="left", padx=px(4))
        self._lbl(ctl, "BAUD", bg=BG).pack(side="left", padx=(px(8), px(4)))
        self.baud = ttk.Combobox(ctl, width=7, values=["115200", "230400", "460800", "921600"], font=(F["ui"], 10))
        self.baud.set("115200")
        self.baud.pack(side="left")
        self._lbl(ctl, "CAN", bg=BG).pack(side="left", padx=(px(8), px(4)))
        self.can = ttk.Combobox(ctl, width=4, values=["250", "500"], font=(F["ui"], 10))
        self.can.set("250")
        self.can.pack(side="left")
        self.btn_conn = mkbtn(ctl, "CONNECT", self.toggle_conn, bg=ACCENT, fg=BG)
        self.btn_conn.pack(side="left", padx=(px(12), 0))

        # ---- body
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=px(10), pady=(0, px(10)))
        g = dict(padx=px(6), pady=px(6), sticky="nsew")

        p, _, inner = self.panel(body, "ENGINE SPEED")
        p.grid(row=0, column=0, **g)
        self.g_rpm = ArcGauge(inner, "Engine RPM", "rpm", 0, 2500, 320, warn=2200, crit=2400, majors=5)
        self.g_rpm.pack()
        tm = tk.Frame(inner, bg=PANEL)
        tm.pack(fill="x", pady=(px(4), 0))
        self._lbl(tm, "TORQUE MODE").pack(side="left")
        self.tq_lbl = tk.Label(tm, text="--", bg=PANEL, fg=TEXT, font=(F["ui"], 10, "bold"))
        self.tq_lbl.pack(side="left", padx=px(8))

        p, _, inner = self.panel(body, "VITALS")
        p.grid(row=0, column=1, **g)
        self.gauges = {
            "oilpress": ArcGauge(inner, "Oil Pressure", "psi", 0, 100, 178, lwarn=15, lcrit=8, majors=5, labels=False),
            "coolant": ArcGauge(inner, "Coolant Temp", "°F", 100, 260, 178, warn=215, crit=225, majors=4,
                                labels=False, low_needs_run=False),
            "boost": ArcGauge(inner, "Boost", "psi", 0, 40, 178, majors=4, labels=False),
            "load": ArcGauge(inner, "Engine Load", "%", 0, 100, 178, warn=95, majors=5, labels=False),
        }
        for i, w in enumerate(self.gauges.values()):
            w.grid(row=i // 2, column=i % 2, padx=px(2), pady=px(2))

        p, _, inner = self.panel(body, "READINGS")
        p.grid(row=0, column=2, **g)
        T = lambda *a, **k: Tile(inner, *a, bw=150, bh=74, **k)
        self.tiles = {
            "oiltemp": T("Oil Temp", "°F", lo=100, hi=280, warn=240, crit=260, low_needs_run=False),
            "fueltemp": T("Fuel Temp", "°F", lo=40, hi=200, warn=160, crit=180, low_needs_run=False),
            "intake": T("Intake Temp", "°F", lo=0, hi=250, warn=180, crit=210, low_needs_run=False),
            "fuelrate": T("Fuel Rate", "gal/h", fmt="{:.1f}", lo=0, hi=20),
            "fuelpress": T("Fuel Press", "psi", lo=0, hi=150),
            "battery": T("Battery", "V", fmt="{:.1f}", lo=18, hi=32, warn=29.5, crit=31, lwarn=23.5,
                         lcrit=22, low_needs_run=False),
            "torque": T("Torque", "%", lo=0, hi=100),
            "pedal": T("Pedal", "%", lo=0, hi=100),
            "coollvl": T("Coolant Lvl", "%", lo=0, hi=100, lwarn=80, lcrit=50, low_needs_run=False),
            "ecmtemp": T("ECM Temp", "°F", lo=0, hi=250, warn=200, crit=225, low_needs_run=False),
            "hours": T("Engine Hours", "h", fmt="{:.1f}"),
            "fuelused": T("Fuel Used", "gal", fmt="{:,.0f}"),
        }
        for i, w in enumerate(self.tiles.values()):
            w.grid(row=i // 3, column=i % 3, padx=px(3), pady=px(3))

        # ---- throttle
        p, hdr2, inner = self.panel(body, "THROTTLE  ·  TSC1 ENGINE SPEED CONTROL")
        p.grid(row=1, column=0, columnspan=2, **g)
        self.badge = tk.Label(hdr2, text="DISARMED", bg=PANEL2, fg=MUTED, font=(F["ui"], 8, "bold"),
                              padx=px(10), pady=px(2))
        self.badge.pack(side="right")

        req = tk.Frame(inner, bg=PANEL)
        req.grid(row=0, column=0, sticky="w", padx=(0, px(14)))
        self._lbl(req, "REQUESTED").pack(anchor="w")
        self.req_lbl = tk.Label(req, text="750", bg=PANEL, fg=DIM, font=(F["dig"], 34), width=4, anchor="w")
        self.req_lbl.pack(anchor="w")

        mid = tk.Frame(inner, bg=PANEL)
        mid.grid(row=0, column=1, sticky="ew")
        inner.columnconfigure(1, weight=1)
        self.rpm_var = tk.DoubleVar(value=750)
        self.slider = ttk.Scale(mid, from_=700, to=1800, orient="horizontal", variable=self.rpm_var,
                                style="Rpm.Horizontal.TScale", length=px(420), command=self._slide)
        self.slider.pack(fill="x", pady=(px(10), px(10)))
        pre = tk.Frame(mid, bg=PANEL)
        pre.pack(anchor="w")
        mkbtn(pre, "IDLE", lambda: self.set_req(None)).pack(side="left", padx=(0, px(4)))
        for r in (1000, 1400, 1800):
            mkbtn(pre, str(r), lambda r=r: self.set_req(r)).pack(side="left", padx=px(4))
        mkbtn(pre, "−50", lambda: self.nudge(-50)).pack(side="left", padx=(px(16), px(4)))
        mkbtn(pre, "+50", lambda: self.nudge(50)).pack(side="left", padx=px(4))

        self.btn_stop = mkbtn(inner, "STOP\nESC · SPACE", self.stop, bg=CRIT, fg="#ffffff",
                              font=(F["ui"], 15, "bold"))
        self.btn_stop.grid(row=0, column=2, rowspan=2, sticky="nsew", padx=(px(14), 0))

        cfg = tk.Frame(inner, bg=PANEL)
        cfg.grid(row=1, column=0, columnspan=2, sticky="w", pady=(px(12), 0))
        self.e_sa, self.e_da, self.e_min, self.e_max = (mkentry(cfg, v) for v in ("3", "0", "700", "1800"))
        for lbl, e in (("MY SA", self.e_sa), ("ENGINE SA", self.e_da), ("MIN", self.e_min), ("MAX", self.e_max)):
            self._lbl(cfg, lbl).pack(side="left", padx=(0, px(4)))
            e.pack(side="left", padx=(0, px(12)), ipady=px(3))
        self.btn_arm = mkbtn(cfg, "ARM", self.arm, bg=PANEL2, fg=ACCENT)
        self.btn_arm.pack(side="left", padx=(px(4), 0))

        self.resp_lbl = tk.Label(inner, text="Arm with the engine running. Watch torque mode / RPM for the ECM's response.",
                                 bg=PANEL, fg=MUTED, font=(F["ui"], 9), anchor="w")
        self.resp_lbl.grid(row=2, column=0, columnspan=3, sticky="w", pady=(px(10), 0))

        # ---- faults
        p, _, inner = self.panel(body, "FAULTS  ·  DM1")
        p.grid(row=1, column=2, **g)
        lamps = tk.Frame(inner, bg=PANEL)
        lamps.pack(anchor="w")
        self.lamps = {k: Lamp(lamps, lbl, col) for k, lbl, col in
                      (("PROTECT", "PROTECT", WARN), ("AMBER", "WARNING", WARN), ("STOP", "STOP", CRIT),
                       ("MIL", "MIL", WARN), ("WIF", "WIF", CRIT))}
        for w in self.lamps.values():
            w.pack(side="left")
        self.dtc = tk.Text(inner, height=5, width=48, bg=PANEL2, fg=TEXT, relief="flat", bd=0,
                           highlightthickness=1, highlightbackground=BORDER, font=(F["mono"], 9),
                           padx=px(8), pady=px(6), wrap="word")
        self.dtc.pack(fill="both", expand=True, pady=(px(6), px(8)))
        self.dtc.insert("end", "No data")
        self.dtc.config(state="disabled")
        mkbtn(inner, "READ HOURS & FUEL", self.read_totals).pack(anchor="w")

        # ---- log
        p, _, inner = self.panel(body, "LOG")
        p.grid(row=2, column=0, columnspan=3, **g)
        self.log = tk.Text(inner, height=5, bg=BG, fg=MUTED, relief="flat", bd=0, highlightthickness=0,
                           font=(F["mono"], 9), padx=px(6), pady=px(4))
        self.log.pack(fill="both", expand=True)
        for c in range(3):
            body.columnconfigure(c, weight=1)
        body.rowconfigure(2, weight=1)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _widen_dropdown(cb):
        """Make the combobox's drop-down list as wide as its longest entry."""
        vals = cb.cget("values") or ()
        if not vals:
            return
        f = tkfont.Font(font=cb.cget("font"))
        need = max(f.measure(str(v)) for v in vals) + px(34)       # text + scrollbar/padding
        extra = max(0, need - cb.winfo_width())
        # postoffset = (dx, dy, dwidth, dheight): grow the list leftward so it stays on screen
        ttk.Style(cb).configure(cb.cget("style") or "TCombobox", postoffset=(-extra, 0, extra, 0))

    def ports(self):
        real = [p.device for p in serial.tools.list_ports.comports()] if serial else []
        self.rp_map = {}
        if os.name == "nt":
            try:
                self.rp_map = rp1210_devices()
            except Exception:
                self.rp_map = {}
        return real + list(self.rp_map) + ["DEMO"]

    def logmsg(self, s):
        self.log.insert("end", time.strftime("%H:%M:%S  ") + s + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "100.0")
        self.log.see("end")

    def _ints(self):
        return (int(self.e_sa.get(), 0), int(self.e_da.get(), 0), int(self.e_min.get()), int(self.e_max.get()))

    def _limits(self):
        try:
            _, _, lo, hi = self._ints()
            return lo, hi
        except ValueError:
            return 700, 1800

    def _typing(self, e):
        return isinstance(e.widget, (tk.Entry, ttk.Combobox, tk.Text))

    def _space(self, e):
        if not isinstance(e.widget, (tk.Entry, ttk.Combobox)):
            self.stop()

    def _nudge_key(self, e, d):
        if not self._typing(e):
            self.nudge(d)

    def _slide(self, v):
        q = round(float(v) / 25) * 25
        if q != self.rpm_var.get():
            self.rpm_var.set(q)
        self.schedule_update()

    def set_req(self, rpm):
        lo, hi = self._limits()
        self.rpm_var.set(lo if rpm is None else max(lo, min(hi, rpm)))
        self.schedule_update()

    def nudge(self, d):
        self.set_req(self.rpm_var.get() + d)

    # ------------------------------------------------------------ connection
    def toggle_conn(self):
        if self.gw:
            return self.disconnect()
        port = self.port.get()
        try:
            if port == "DEMO":
                self.gw = DemoGateway(self.q)
            elif port in getattr(self, "rp_map", {}):
                dll, dev = self.rp_map[port]
                self.gw = RP1210Gateway(dll, dev, int(self.can.get()), self.q)
            elif serial is None:
                return messagebox.showerror("Serial", "pyserial not installed:  pip install pyserial")
            else:
                self.gw = Gateway(port, int(self.baud.get()), self.q)
        except Exception as e:
            return messagebox.showerror("Connect", str(e))
        self.claimed, self.sa = False, None
        self.vals, self.stamp, self.dm1 = {}, {}, {}
        self.rx_count, self.ack_count, self.seen, self.heart_errs = 0, 0, set(), None
        self.gw.send(bytes([SETACK, 1]))
        self.gw.send(bytes([REQINFO, VERSION]))
        self.gw.reset(int(self.can.get()))
        self.after(300, self.add_filters)
        self.after(600, lambda: self.gw and self.gw.send(bytes([REQINFO, REPSTATUS])))
        self.after(5000, self.diag_report)
        self.btn_conn.config(text="DISCONNECT")
        self.btn_conn.recolor(PANEL2, TEXT)
        self.logmsg(f"Opened {port} - reset @ {self.can.get()} kbps, listen-only")

    def add_filters(self):
        if self.gw:
            self.gw.add_filter(0x100000)   # pass all PGNs

    def diag_report(self):
        if not self.gw:
            return
        self.logmsg(f"Diag after 5 s: {self.rx_count} J1939 frames, {self.ack_count} gateway ACKs, "
                    f"{len(self.seen)} unique PGN/SA")
        if self.rx_count == 0:
            self.logmsg("  No J1939 traffic. Check key ON, CAN-H/CAN-L, 120 ohm at each end, 250 kbps.")

    def disconnect(self):
        self.stop()
        if self.gw:
            try:
                self.gw.reset(int(self.can.get()))
            except Exception:
                pass
            self.gw.close()
        self.gw, self.claimed, self.sa = None, False, None
        self.btn_conn.config(text="CONNECT")
        self.btn_conn.recolor(ACCENT, BG)

    # ------------------------------------------------------------ throttle
    def _claim(self, sa):
        self.gw.claim(NAME, sa)
        self.logmsg(f"Claiming address {sa} ...")
        self.badge.config(text="CLAIMING…", bg=WARN, fg=BG)

    def arm(self):
        if not self.gw:
            return messagebox.showwarning("Arm", "Connect first.")
        rpm = self.vals.get("rpm") if time.time() - self.stamp.get("rpm", 0) < 1.5 else None
        if rpm is None or rpm < 400:
            return messagebox.showwarning("Arm", "Engine must be running (live RPM > 400).")
        try:
            sa, da, lo, hi = self._ints()
        except ValueError:
            return messagebox.showerror("Arm", "Bad SA/RPM values.")
        if sa == da or sa == 0 or not 0 < sa <= 253:
            return messagebox.showerror("Arm", "My SA must be 1-253 and differ from Engine SA (engine is normally 0).")
        if not 300 <= lo < hi <= 3000:
            return messagebox.showerror("Arm", "Check Min/Max RPM.")
        self.slider.config(from_=lo, to=hi)
        self.rpm_var.set(max(lo, min(hi, round(rpm / 25) * 25)))   # start at current speed
        if self.claimed and self.sa == sa:
            self.start_throttle()
        else:
            self.pending_arm = True
            self._claim(sa)

    def start_throttle(self):
        self.armed, self.pending_arm = True, False
        self.arm_time, self.base_mode = time.time(), self.vals.get("tqmode")
        self.arm_rpm = self.vals.get("rpm") or 0
        self.badge.config(text="● ARMED  ·  TSC1 ACTIVE", bg=CRIT, fg="#ffffff")
        self.btn_arm.recolor(blend(PANEL2, CRIT, 0.25), "#ffffff")
        self.send_tsc1()
        self.logmsg("TSC1 speed control ACTIVE")

    def schedule_update(self):
        if self.armed and not self._upd_pending:
            self._upd_pending = True
            self.after(50, self.send_tsc1)

    def send_tsc1(self):
        self._upd_pending = False
        if not (self.armed and self.gw):
            return
        _, da, lo, hi = self._ints()
        rpm = max(lo, min(hi, self.rpm_var.get()))
        self.gw.tx_periodic(PGN_TSC1, da, self.sa, 3, 10, tsc1(rpm))

    def stop(self, reason=None):
        was = self.armed or self.pending_arm
        self.armed = self.pending_arm = False
        self.badge.config(text="DISARMED", bg=PANEL2, fg=MUTED)
        self.btn_arm.recolor(PANEL2, ACCENT)
        if self.gw and self.sa is not None:
            try:
                da = int(self.e_da.get(), 0)
                self.gw.tx_periodic(PGN_TSC1, da, self.sa, 3, 0, tsc1(None))      # delete periodic
                for _ in range(3):
                    self.gw.tx(PGN_TSC1, da, self.sa, 3, tsc1(None))              # release override
            except Exception as e:
                self.logmsg(f"STOP send error: {e}")
        if was:
            self.logmsg("STOP" + (f": {reason}" if reason else ""))

    def read_totals(self):
        if not self.gw:
            return messagebox.showwarning("Read", "Connect first.")
        if self.claimed:
            self.last_req = 0
            return
        try:
            sa, da, _, _ = self._ints()
        except ValueError:
            return messagebox.showerror("Read", "Bad SA values.")
        if sa == da or sa == 0:
            return messagebox.showerror("Read", "My SA must be 1-253 and differ from Engine SA.")
        self.pending_read = True
        self._claim(sa)

    # ------------------------------------------------------------ incoming
    def poll(self):
        self.drain()
        self.after(20, self.poll)

    def drain(self):
        try:
            while True:
                kind, m, t = self.q.get_nowait()
                if kind == "err":
                    self.logmsg("Adapter error: " + m)
                    self.disconnect()
                    return
                if kind == "log":
                    self.logmsg(m)
                    continue
                if kind == "update":
                    self._on_update_result(*m)
                    continue
                if kind == "disarm":
                    if self.armed or self.pending_arm:
                        self.stop(m)
                    continue
                self.handle(m, t)
        except queue.Empty:
            pass

    def handle(self, m, t):
        mid = m[0]
        if mid in (RXDATA, RXDATAX) and len(m) >= 7:
            pgn = (m[1] << 16) | (m[2] << 8) | m[3]
            sa = m[5]
            data = m[7:-8] if mid == RXDATAX else m[7:]
            self.rx_count += 1
            self.rate_n += 1
            if (pgn, sa) not in self.seen and len(self.seen) < 300:
                self.seen.add((pgn, sa))
                self.logmsg(f"New: PGN 0x{pgn:05X} ({pgn}) SA {sa} -> DA {m[4]}  "
                            f"[{' '.join(f'{b:02X}' for b in data[:8])}]")
            if pgn == PGN_DM1 and len(data) >= 6:
                self.dm1[sa] = (*parse_dm1(data), t)
                return
            dec = DECODERS.get(pgn)
            if dec and len(data) >= 8:
                for k, v in dec(data).items():
                    if v is not None:
                        self.vals[k], self.stamp[k] = v, t
        elif mid == ACK:
            self.ack_count += 1
        elif mid == VERSION and len(m) >= 7:
            self.logmsg(f"Gateway VERSION: HW {m[1]}.{m[2]:02}.{m[3]:02}  FW {m[4]}.{m[5]:02}.{m[6]:02}")
        elif mid == HEART:
            if len(m) >= 9 and (m[7], m[8]) != self.heart_errs:
                self.logmsg(f"Heartbeat: FW {m[4]}.{m[5]:02}.{m[6]:02}, gateway RX errors "
                            f"checksum={m[7]} stuffing={m[8]}")
                self.heart_errs = (m[7], m[8])
            self.last_heart = t
        elif mid == REPSTATUS and len(m) >= 3:
            st, sa = m[1], m[2]
            names = {1: "claim in progress", 2: "claimed", 3: "claim failed", 4: "listen-only"}
            self.logmsg(f"Gateway status {st} ({names.get(st, '?')}), SA {sa}")
            if st == 2:
                self.claimed, self.sa = True, sa
                self.add_filters()
                if self.pending_read:
                    self.pending_read, self.last_req = False, 0
                if self.pending_arm:
                    self.start_throttle()
                elif not self.armed:
                    self.badge.config(text="DISARMED", bg=PANEL2, fg=MUTED)
            elif st == 3:
                self.pending_arm = self.pending_read = False
                self.badge.config(text="CLAIM FAILED", bg=PANEL2, fg=CRIT)
                self.logmsg("Address claim FAILED (SA in use?)")

    # ------------------------------------------------------------ UI refresh
    def refresh(self):
        self.drain()
        now = time.time()
        if self.gw is not None and hasattr(self.gw, "kick"):
            self.gw.kick()                               # fail-safe heartbeat to the RP1210 TSC1 loop

        def val(k, age=2.5):
            return self.vals.get(k) if now - self.stamp.get(k, 0) < age else None

        rpm = val("rpm")
        running = rpm is not None and rpm > 500
        self.g_rpm.set(rpm, marker=self.rpm_var.get() if self.armed else None)
        for k, w in self.gauges.items():
            w.set(val(k), running)
        for k, w in self.tiles.items():
            w.set(val(k, 20 if k in ("hours", "fuelused") else 2.5), running)

        mode = val("tqmode")
        self.tq_lbl.config(text="--" if mode is None else f"{mode}  ·  {TORQUE_MODES.get(mode, '?')}",
                           fg=ACCENT if mode not in (None, 0) else TEXT)
        self.req_lbl.config(text=f"{self.rpm_var.get():.0f}", fg=ACCENT if self.armed else DIM)

        # ECM response to TSC1
        if self.armed and now - self.arm_time > 2:
            req = self.rpm_var.get()
            changed = mode is not None and mode != self.base_mode
            tracking = rpm is not None and abs(rpm - req) < 75 and abs(req - self.arm_rpm) > 150
            if changed or tracking:
                self.resp_lbl.config(text=f"✔  ECM ACCEPTING TSC1   ·   RPM {rpm or 0:.0f} / requested {req:.0f}", fg=OK)
            else:
                self.resp_lbl.config(text=f"✖  ECM IGNORING TSC1?   ·   RPM {rpm or 0:.0f} / requested {req:.0f}, "
                                          f"torque mode unchanged", fg=CRIT)
        elif self.armed:
            self.resp_lbl.config(text="Waiting for ECM response…", fg=WARN)

        # status pills
        if now - self.rate_t >= 1:
            self.rate, self.rate_n, self.rate_t = self.rate_n / (now - self.rate_t), 0, now
        if self.gw:
            hb = now - self.last_heart < 3
            if isinstance(self.gw, DemoGateway):
                kind = "DEMO"
            elif isinstance(self.gw, RP1210Gateway):
                kind = "RP1210" if hb else "RP1210 (no data)"
            else:
                kind = "OK" if hb else "NO HEARTBEAT"
            self.p_gw.config(text=f"● GATEWAY  {kind}", fg=OK if hb else WARN)
            self.p_bus.config(text=f"● BUS  {self.rate:.0f} msg/s", fg=OK if self.rate > 0 else WARN)
            self.p_mode.config(text=f"● {'SA ' + str(self.sa) if self.claimed else 'LISTEN-ONLY'}",
                               fg=ACCENT if self.claimed else MUTED)
        else:
            self.p_gw.config(text="● GATEWAY  OFFLINE", fg=DIM)
            self.p_bus.config(text="● BUS  --", fg=DIM)
            self.p_mode.config(text="● MODE  --", fg=DIM)

        self._refresh_faults(now, val("wif", 5))

        # periodic requests (hours / fuel used) once an address is claimed
        if self.gw and self.claimed and now - self.last_req > 5:
            self.last_req = now
            try:
                da = int(self.e_da.get(), 0)
                for pg in (PGN_HOURS, PGN_LFC):
                    self.gw.request(pg, da, self.sa)
            except Exception:
                pass

        # safety watchdogs
        if self.armed:
            _, hi = self._limits()
            if now - self.stamp.get("rpm", 0) > 1.5:
                self.stop("lost engine RPM data")
            elif (rpm or 0) > hi + 300:
                self.stop("overspeed")
        self.after(100, self.refresh)

    def _refresh_faults(self, now, wif):
        live = {sa: v for sa, v in self.dm1.items() if now - v[2] < 4}
        if not live:
            for k in ("PROTECT", "AMBER", "STOP", "MIL"):
                self.lamps[k].set(None)
            text = "No DM1 data" if self.gw else "No data"
        else:
            for k in ("PROTECT", "AMBER", "STOP", "MIL"):
                self.lamps[k].set(1 if any(v[0][k] == 1 for v in live.values()) else 0)
            lines = []
            for sa, (_, dtcs, _) in sorted(live.items()):
                for spn, fmi, oc in dtcs:
                    pre = f"SA{sa:<3} " if len(live) > 1 else ""
                    lines.append(f"{pre}SPN {spn:<5} FMI {fmi:<2} OC {oc:<3} "
                                 f"{SPN_NAMES.get(spn, '')} - {FMI_NAMES.get(fmi, '')}")
            text = "\n".join(lines) if lines else "✔ No active faults"
        self.lamps["WIF"].set(None if wif is None else (1 if wif == 1 else 2 if wif == 2 else 0))
        if text != self._dtc_text:
            self._dtc_text = text
            self.dtc.config(state="normal")
            self.dtc.delete("1.0", "end")
            self.dtc.insert("end", text)
            self.dtc.config(state="disabled")

    # ------------------------------------------------------------ updates
    def check_updates(self, manual=False):
        def work():
            try:
                tag, url, _ = check_latest_release()
                self.q.put(("update", (tag, url, manual), time.time()))
            except Exception as e:
                if manual:
                    self.q.put(("log", f"Update check failed: {e}", time.time()))
        threading.Thread(target=work, daemon=True).start()

    def _on_update_result(self, tag, url, manual):
        if tag and _vtuple(tag) > _vtuple(__version__):
            self.update_url = url
            self.p_ver.config(text=f"⬆ UPDATE {tag} AVAILABLE — CLICK", fg=WARN)
            self.logmsg(f"Update available: {tag} (you have v{__version__}). Click the version label to download.")
        elif manual:
            self.p_ver.config(text=f"v{__version__} · UP TO DATE", fg=OK)
            self.logmsg(f"You have the latest version (v{__version__}).")
            self.after(4000, lambda: self.p_ver.config(text=f"v{__version__}", fg=DIM))

    def on_version_click(self):
        if self.update_url:
            webbrowser.open(self.update_url)
        else:
            self.p_ver.config(text=f"v{__version__} · CHECKING…", fg=MUTED)
            self.check_updates(manual=True)

    def on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
