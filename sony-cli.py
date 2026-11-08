#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Switch Sony headphones between Noise Cancelling and Ambient Sound mode.

Protocol extracted from BudsLink (src/lib/devices/sony/sonySocketBase.js,
sonySocketV2.js, sonyConfig.js, profileManager.js). Works with Sony devices
that speak Sony protocol "v2" (e.g. LinkBuds S, WF-1000XM4/XM5, WH-1000XM5)
over an RFCOMM channel that is opened through BlueZ's Profile API, exactly
like BudsLink does it. Which NC / ambient features exist is queried from the
device itself. Mainly tested on the LinkBuds S.

Usage:
    sony-cli.py {toggle|nc|ambient|off|status} [--mac AA:BB:CC:DD:EE:FF] [-v]
    sony-cli.py            (no arguments: print help)

Requires: Python 3, PyGObject (python3-gi), BlueZ, device paired + connected.
The BudsLink app must not be running (it holds the same RFCOMM service).
"""

import argparse
import os
import select
import sys
import time

try:
    from gi.repository import Gio, GLib
except ImportError:
    sys.exit("error: PyGObject is missing (install python3-gi / python-gobject)")

# --- Constants (sonyDevice.js / sonyConfig.js) -------------------------------

SONY_UUID_V1 = "96cc203e-5068-46ad-b32d-e316f5e069ba"   # older protocol, not supported
SONY_UUID_V2 = "956c7b26-d49a-4ba8-b03f-b17d393cb6e2"
PROFILE_PATH = "/io/github/sonycli/profile"

HEADER, TRAILER, ESCAPE, ESCAPE_MASK = 0x3E, 0x3C, 0x3D, 0xEF

MT_ACK, MT_COMMAND_1, MT_COMMAND_2 = 0x01, 0x0C, 0x0E

# PayloadTypeV2T1
CONNECT_GET_PROTOCOL_INFO = 0x00
CONNECT_RET_PROTOCOL_INFO = 0x01
CONNECT_GET_SUPPORT_FUNCTION = 0x06
CONNECT_RET_SUPPORT_FUNCTION = 0x07
NCASM_GET_PARAM = 0x66
NCASM_RET_PARAM = 0x67
NCASM_SET_PARAM = 0x68
NCASM_NTFY_PARAM = 0x69

# AsmType
ASM_NC_AUTO_WIND = 0x15        # MODE_NC_ASM_AUTO_NC_MODE_SWITCH_AND_ASM_SEAMLESS
ASM_NC_DUAL = 0x17             # MODE_NC_ASM_DUAL_NC_MODE_SWITCH_AND_ASM_SEAMLESS
ASM_NC_DUAL_NA = 0x19          # ..._ASM_SEAMLESS_NA (auto ambient / noise adaptation)
ASM_ON_OFF = 0x21
ASM_SEAMLESS = 0x22
ASM_BUTTON = 0x30              # NC_AMB_TOGGLE (button assignment, not the mode)

# FunctionTypeV2T1 -> AsmType, in BudsLink's priority order (sonySocketV2.js)
ASM_FROM_FUNCTION = [
    (0x6D, ASM_NC_DUAL_NA),    # ..._LEVEL_ADJUSTMENT_NOISE_ADAPTATION
    (0x6B, ASM_NC_DUAL),       # MODE_NC_ASM_NOISE_CANCELLING_DUAL_..._LEVEL_ADJUSTMENT
    (0x68, ASM_NC_AUTO_WIND),  # MODE_NC_ASM_NOISE_CANCELLING_DUAL_AUTO_..._LEVEL_ADJUSTMENT
    (0x67, ASM_SEAMLESS),      # AMBIENT_SOUND_MODE_LEVEL_ADJUSTMENT
    (0x66, ASM_ON_OFF),        # AMBIENT_SOUND_MODE_ONOFF
]

# AmbientSoundMode
OFF, NC, WIND, AMBIENT = "off", "nc", "wind", "ambient"
MODE_LABEL = {OFF: "Off", NC: "Noise Cancelling", WIND: "Wind Noise Reduction",
              AMBIENT: "Ambient Sound"}

VERBOSE = False


def log(msg):
    if VERBOSE:
        print(msg, file=sys.stderr)


def hexs(data):
    return " ".join(f"{b:02x}" for b in data)


class SonyError(Exception):
    pass


# --- Bluetooth: find device & open RFCOMM via BlueZ Profile API --------------

PROFILE_XML = """
<node>
  <interface name="org.bluez.Profile1">
    <method name="Release"/>
    <method name="NewConnection">
      <arg type="o" name="device" direction="in"/>
      <arg type="h" name="fd" direction="in"/>
      <arg type="a{sv}" name="fd_properties" direction="in"/>
    </method>
    <method name="RequestDisconnection">
      <arg type="o" name="device" direction="in"/>
    </method>
  </interface>
</node>
"""


def find_device(bus, mac):
    reply = bus.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
                          "GetManagedObjects", None, None,
                          Gio.DBusCallFlags.NONE, -1, None)
    objects = reply.unpack()[0]

    candidates = []   # devices speaking Sony protocol v2
    v1_only = []      # Sony devices that only speak the older v1 protocol
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        address = dev.get("Address", "")
        name = dev.get("Name") or dev.get("Alias") or address
        uuids = [u.lower() for u in dev.get("UUIDs", [])]
        if mac and address.upper() != mac.upper():
            continue
        if SONY_UUID_V2 in uuids:
            candidates.append((path, name, dev))
        elif SONY_UUID_V1 in uuids:
            v1_only.append(name)
        elif mac:
            raise SonyError(f"'{name}' ({address}) does not offer the Sony v2 "
                            "control service - not a supported Sony device")

    if not candidates:
        if v1_only:
            raise SonyError(f"'{v1_only[0]}' only speaks the older Sony v1 protocol, "
                            "which is not supported")
        what = mac if mac else "a paired Sony device"
        raise SonyError(f"could not find {what} in BlueZ")

    connected = [c for c in candidates if c[2].get("Connected")]
    if not connected:
        raise SonyError(f"'{candidates[0][1]}' is paired but not connected")
    if len(connected) > 1:
        names = ", ".join(f"{n} ({d.get('Address', '?')})" for _, n, d in connected)
        raise SonyError(f"several Sony devices are connected: {names} - pick one with --mac")

    path, name, dev = connected[0]
    log(f"using device {name} ({path})")
    return path, name, dev.get("Address", "?")


def open_rfcomm(bus, device_path, timeout=15):
    """Register a client Profile1 for the Sony v2 UUID, ConnectProfile, and
    return the RFCOMM fd BlueZ hands us via NewConnection."""
    state = {"fd": None, "error": None}
    loop = GLib.MainLoop()

    def on_method(_conn, _sender, _path, _iface, method, params, invocation):
        if method == "NewConnection":
            idx = params.get_child_value(1).get_handle()
            fd_list = invocation.get_message().get_unix_fd_list()
            state["fd"] = fd_list.get(idx)  # returns a dup we own
            log(f"NewConnection: fd={state['fd']}")
            invocation.return_value(None)
            loop.quit()
        else:
            invocation.return_value(None)

    iface_info = Gio.DBusNodeInfo.new_for_xml(PROFILE_XML).interfaces[0]
    reg_id = bus.register_object(PROFILE_PATH, iface_info,
                                  on_method, None, None)

    try:
        opts = {
            "Name": GLib.Variant("s", "sony-cli"),
            "Role": GLib.Variant("s", "client"),
            "AutoConnect": GLib.Variant("b", False),
        }
        bus.call_sync("org.bluez", "/org/bluez", "org.bluez.ProfileManager1",
                      "RegisterProfile",
                      GLib.Variant("(osa{sv})", (PROFILE_PATH, SONY_UUID_V2, opts)),
                      None, Gio.DBusCallFlags.NONE, -1, None)
    except GLib.Error as e:
        bus.unregister_object(reg_id)
        remote = Gio.DBusError.get_remote_error(e) or ""
        if remote in ("org.bluez.Error.AlreadyExists", "org.bluez.Error.NotPermitted"):
            raise SonyError("the Sony service UUID is already registered by another "
                            "program - quit BudsLink (or similar) and try again")
        raise SonyError(f"RegisterProfile failed: {e.message}")

    def unregister():
        try:
            bus.call_sync("org.bluez", "/org/bluez", "org.bluez.ProfileManager1",
                          "UnregisterProfile", GLib.Variant("(o)", (PROFILE_PATH,)),
                          None, Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error:
            pass
        bus.unregister_object(reg_id)

    def on_connected(conn, res):
        try:
            conn.call_finish(res)
            log("ConnectProfile OK")
        except GLib.Error as e:
            if state["fd"] is None:
                state["error"] = e.message
                loop.quit()

    def on_timeout():
        state["timer"] = None
        state["error"] = state["error"] or "timed out waiting for the RFCOMM connection"
        loop.quit()
        return GLib.SOURCE_REMOVE

    bus.call("org.bluez", device_path, "org.bluez.Device1", "ConnectProfile",
             GLib.Variant("(s)", (SONY_UUID_V2,)), None,
             Gio.DBusCallFlags.NONE, timeout * 1000, None, on_connected)
    state["timer"] = GLib.timeout_add_seconds(timeout, on_timeout)
    loop.run()
    if state["timer"] is not None:
        GLib.source_remove(state["timer"])

    if state["fd"] is None:
        unregister()
        raise SonyError(f"ConnectProfile failed: {state['error']}")

    return state["fd"], unregister


# --- Sony framing (sonySocketBase.js) ----------------------------------------

def checksum(data):
    return sum(data) & 0xFF


def escape(data):
    out = bytearray()
    for b in data:
        if b in (HEADER, TRAILER, ESCAPE):
            out += bytes((ESCAPE, b & ESCAPE_MASK))
        else:
            out.append(b)
    return out


def unescape(data):
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == ESCAPE:
            b = next(it, 0) | (~ESCAPE_MASK & 0xFF)
        out.append(b)
    return out


class SonyLink:
    def __init__(self, fd):
        self.fd = fd
        self.seq = 0
        self.buf = bytearray()

    # -- low level --
    def _write_frame(self, msg_type, payload, seq):
        body = bytes((msg_type, seq)) + len(payload).to_bytes(4, "big") + bytes(payload)
        frame = bytes((HEADER,)) + escape(body) + escape((checksum(body),)) + bytes((TRAILER,))
        log(f"TX {hexs(frame)}")
        os.write(self.fd, frame)

    def send(self, msg_type, payload):
        seq = self.seq
        self.seq = 1 - self.seq
        self._write_frame(msg_type, payload, seq)

    def _read_frame(self, deadline):
        """Return the next decoded (type, seq, payload) or None on timeout.
        Incoming COMMAND frames are ACKed immediately, like BudsLink does."""
        while True:
            start = self.buf.find(bytes((HEADER,)))
            if start >= 0:
                end = self.buf.find(bytes((TRAILER,)), start + 1)
                if end >= 0:
                    raw = bytes(self.buf[start:end + 1])
                    del self.buf[:end + 1]
                    log(f"RX {hexs(raw)}")
                    decoded = self._decode(raw)
                    if decoded is None:
                        continue
                    msg_type, seq, payload = decoded
                    if msg_type in (MT_COMMAND_1, MT_COMMAND_2):
                        self._write_frame(MT_ACK, b"", 1 - seq)
                    return decoded
            else:
                self.buf.clear()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self.fd, 4096)
            if not chunk:
                raise SonyError("connection closed by the headphones")
            self.buf += chunk

    @staticmethod
    def _decode(raw):
        inner = unescape(raw[1:-1])
        if len(inner) < 7:
            log("  (frame too short, ignored)")
            return None
        body, chk = inner[:-1], inner[-1]
        if checksum(body) != chk:
            log("  (checksum mismatch, ignored)")
            return None
        length = int.from_bytes(body[2:6], "big")
        return body[0], body[1], bytes(body[6:6 + length])

    # -- high level --
    def request(self, payload, match, msg_type=MT_COMMAND_1, timeout=3.0, retries=3):
        """Send payload, wait for a COMMAND reply where match(payload) is true."""
        for attempt in range(retries):
            self.send(msg_type, payload)
            deadline = time.monotonic() + timeout
            while True:
                frame = self._read_frame(deadline)
                if frame is None:
                    log(f"  no reply (attempt {attempt + 1}/{retries})")
                    break
                ftype, _, fpayload = frame
                if ftype == msg_type and fpayload and match(fpayload):
                    return fpayload
        raise SonyError(f"no reply to request {hexs(payload)}")

    def query(self, payload, msg_type=MT_COMMAND_1, timeout=1.5, retries=1):
        """GET that tolerates no answer: reply must be RET (req+1) or NTFY
        (req+3) with the same sub-type byte. Returns payload or None."""
        want = (payload[0] + 1, payload[0] + 3)
        try:
            return self.request(payload, lambda p: len(p) > 1 and p[0] in want
                                and p[1] == payload[1],
                                msg_type=msg_type, timeout=timeout, retries=retries)
        except SonyError:
            log(f"  no answer to {hexs(payload)}, skipping")
            return None

    def send_and_wait_ack(self, payload, timeout=2.0):
        self.send(MT_COMMAND_1, payload)
        deadline = time.monotonic() + timeout
        while True:
            frame = self._read_frame(deadline)
            if frame is None:
                return False
            if frame[0] == MT_ACK:
                return True


# --- NC / ASM logic (sonySocketV2.js) -----------------------------------------

def get_asm_type(link):
    link.request([CONNECT_GET_PROTOCOL_INFO, 0x00],
                 lambda p: p[0] == CONNECT_RET_PROTOCOL_INFO)

    p = link.request([CONNECT_GET_SUPPORT_FUNCTION, 0x00],
                     lambda p: p[0] == CONNECT_RET_SUPPORT_FUNCTION)
    if len(p) < 3:
        raise SonyError("malformed support-function reply")
    count = p[2]
    funcs = {p[3 + i * 2] for i in range(count) if 3 + i * 2 < len(p)}
    log("support functions: " + " ".join(f"{f:02x}" for f in sorted(funcs)))

    for func, asm in ASM_FROM_FUNCTION:
        if func in funcs:
            log(f"asm type: 0x{asm:02x}")
            return asm, funcs
    raise SonyError("device reports no noise-cancelling / ambient-sound support")


def parse_state(asm, p):
    if len(p) < 6 or len(p) > 9 or p[1] != asm:
        raise SonyError(f"unexpected NC/ASM reply: {hexs(p)}")

    no_nc = asm in (ASM_SEAMLESS, ASM_ON_OFF)
    has_wind = asm == ASM_NC_AUTO_WIND
    has_na = asm == ASM_NC_DUAL_NA

    if p[3] == 0x00:
        mode = OFF
    elif has_wind:
        if p[5] in (0x03, 0x05):
            mode = WIND
        elif p[5] == 0x02:
            mode = NC if p[4] == 0x00 else AMBIENT
        else:
            raise SonyError(f"unknown NC/ASM sub-mode in reply: {hexs(p)}")
    elif no_nc:
        mode = AMBIENT
    else:
        mode = NC if p[4] == 0x00 else AMBIENT

    i = len(p) - (4 if has_na else 2)
    state = {
        "mode": mode,
        "focus_voice": p[i] == 0x01,
        "level": p[i + 1] if 0 <= p[i + 1] <= 20 else 10,
        "auto_ambient": False,
        "sensitivity": 0x00,
    }
    if has_na:
        state["auto_ambient"] = p[i + 2] == 0x01
        state["sensitivity"] = p[i + 3] if p[i + 3] in (0, 1, 2) else 0x00
    return state


def get_state(link, asm):
    p = link.request([NCASM_GET_PARAM, asm],
                     lambda p: p[0] in (NCASM_RET_PARAM, NCASM_NTFY_PARAM)
                     and len(p) > 1 and p[1] == asm)
    return parse_state(asm, p)


def set_state(link, asm, mode, st):
    no_nc = asm in (ASM_SEAMLESS, ASM_ON_OFF)
    payload = [NCASM_SET_PARAM, asm, 0x01, 0x00 if mode == OFF else 0x01]
    if not no_nc:
        payload.append(0x01 if mode == AMBIENT else 0x00)
    if asm == ASM_NC_AUTO_WIND:
        payload.append(0x03 if mode == WIND else 0x02)
    payload.append(0x01 if st["focus_voice"] else 0x00)
    payload.append(max(1, st["level"]))
    if asm == ASM_NC_DUAL_NA:
        payload.append(0x01 if st["auto_ambient"] else 0x00)
        payload.append(st["sensitivity"])

    if not link.send_and_wait_ack(payload):
        log("  no ACK for SET (checking state anyway)")


# --- Extended status (read-only GETs from sonySocketV2.js) --------------------

CODECS = {0x00: "Unsettled", 0x01: "SBC", 0x02: "AAC", 0x10: "LDAC", 0x20: "aptX",
          0x21: "aptX HD", 0x30: "LC3", 0xFF: "Other"}
DSEE_TYPES = {0x00: "DSEE HX", 0x01: "DSEE", 0x02: "DSEE HX AI", 0x03: "DSEE Ultimate"}
EQ_PRESETS = {0x00: "Off", 0x10: "Bright", 0x11: "Excited", 0x12: "Mellow", 0x13: "Relaxed",
              0x14: "Vocal", 0x15: "Treble Boost", 0x16: "Bass Boost", 0x17: "Speech",
              0xA0: "Manual", 0xA1: "Custom 1", 0xA2: "Custom 2"}
EQ6_LABELS = ["Bass", "400", "1k", "2.5k", "6.3k", "16k"]
EQ10_LABELS = ["31", "63", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
BUTTON_MODES = {0x00: "Ambient Sound Control", 0x10: "Volume Control",
                0x20: "Playback Control", 0x22: "Playback Control",
                0x35: "Ambient Sound Control (Quick Access)", 0xFF: "Not assigned"}
AMB_BUTTON = {0x01: "NC -> Ambient -> Off", 0x02: "NC <-> Ambient",
              0x03: "NC <-> Off", 0x04: "Ambient <-> Off"}
S2C_SENS = {0x00: "Auto", 0x01: "High", 0x02: "Low"}
S2C_TIMEOUT = {0x00: "Short", 0x01: "Standard", 0x02: "Long", 0x03: "Off"}
AUTO_ASM_SENS = {0x00: "Standard", 0x01: "High", 0x02: "Low"}
POWER_OFF_TIME = {0x00: "5 min", 0x01: "30 min", 0x02: "1 h", 0x03: "3 h"}

# FunctionTypeV2T1 -> EqInquiryType, BudsLink priority order
EQ_TYPE_FROM_FUNCTION = [(0x50, 0x00), (0x51, 0x01), (0x52, 0x02), (0x57, 0x04), (0x55, 0x31)]


def onoff(v):
    return "on" if v else "off"


def bool_byte(b):
    """booleanFromByte: 0 -> False, 1 -> True, else None."""
    return {0x00: False, 0x01: True}.get(b)


def battery_text(level, status):
    level = max(0, min(level, 100))
    return f"{level}%" + (" (charging)" if status in (0x01, 0x03) else "")


def get_battery(link, funcs):
    parts = []
    single = (0x08 if 0x28 in funcs else 0x00 if 0x20 in funcs else None)
    dual = (0x09 if 0x29 in funcs else 0x01 if 0x21 in funcs else None)
    case = (0x0A if 0x2A in funcs else 0x02 if 0x22 in funcs else None)

    if single is not None:
        p = link.query([0x22, single])
        if p and len(p) >= 4:
            parts.append(battery_text(p[2], p[3]))
    if dual is not None:
        p = link.query([0x22, dual])
        if p and len(p) >= 6:
            parts.append("L " + (battery_text(p[2], p[3]) if p[2] > 0 else "-"))
            parts.append("R " + (battery_text(p[4], p[5]) if p[4] > 0 else "-"))
    if case is not None:
        p = link.query([0x22, case])
        if p and len(p) >= 4:
            parts.append("Case " + (battery_text(p[2], p[3]) if p[2] > 0 else "-"))
    return "  ".join(parts) or None


def collect_status(link, asm, funcs, st):
    """Return list of (label, value) lines; entries without an answer are skipped."""
    out = []

    p = link.query([0x04, 0x02])  # CONNECT_GET_DEVICE_INFO, FW_VERSION
    if p and len(p) >= 3 and len(p) >= 3 + p[2]:
        out.append(("Firmware", bytes(p[3:3 + p[2]]).decode(errors="replace")))

    batt = get_battery(link, funcs)
    if batt:
        out.append(("Battery", batt))

    if 0x12 in funcs:  # CODEC_INDICATOR
        p = link.query([0x12, 0x02])
        if p and len(p) >= 3 and p[2] in CODECS:
            out.append(("Codec", CODECS[p[2]]))
    if 0x13 in funcs:  # UPSCALING_INDICATOR
        p = link.query([0x12, 0x03])
        if p and len(p) >= 4 and p[2] in DSEE_TYPES:
            out.append(("Upscaling", f"{DSEE_TYPES[p[2]]} "
                        f"{'active' if p[3] != 0x00 else 'inactive'}"))

    out.append(("Noise control", MODE_LABEL[st["mode"]]))
    if asm not in (ASM_ON_OFF,):
        out.append(("  Ambient level", f"{st['level']}/20"))
    out.append(("  Focus on voice", onoff(st["focus_voice"])))
    if asm == ASM_NC_DUAL_NA:
        out.append(("  Auto ambient", f"{onoff(st['auto_ambient'])} "
                    f"(sensitivity {AUTO_ASM_SENS.get(st['sensitivity'], '?')})"))
    p = link.query([NCASM_GET_PARAM, ASM_BUTTON])
    if p and len(p) >= 3 and p[2] in AMB_BUTTON:
        out.append(("  Button cycles", AMB_BUTTON[p[2]]))

    eq_type = next((t for f, t in EQ_TYPE_FROM_FUNCTION if f in funcs), 0x01)
    p = link.query([0x56, eq_type])  # EQEBB_GET_PARAM
    if p and len(p) >= 3 and p[2] in EQ_PRESETS:
        text = EQ_PRESETS[p[2]]
        if len(p) >= 10 and p[3] == 0x06:
            bands = [f"{lbl} {p[4 + i] - 10:+d}" for i, lbl in enumerate(EQ6_LABELS)]
            text += "  [" + ", ".join(bands) + "]"
        elif len(p) >= 14 and p[3] == 0x0A:
            bands = [f"{lbl} {p[4 + i] - 6:+d}" for i, lbl in enumerate(EQ10_LABELS)]
            text += "  [" + ", ".join(bands) + "]"
        out.append(("Equalizer", text))

    p = link.query([0xE6, 0x01])  # AUDIO_GET_PARAM: upsampling (DSEE) setting
    if p and len(p) >= 3 and bool_byte(p[2]) is not None:
        out.append(("DSEE", onoff(bool_byte(p[2]))))

    p = link.query([0xF6, 0x03])  # SYSTEM_GET_PARAM: touch button assignment
    if p and len(p) >= 5 and p[2] == 0x02 and p[3] in BUTTON_MODES and p[4] in BUTTON_MODES:
        out.append(("Touch L / R", f"{BUTTON_MODES[p[3]]} / {BUTTON_MODES[p[4]]}"))

    p = link.query([0xF6, 0x0C])  # SYSTEM_GET_PARAM: speak-to-chat (byte = disabled)
    if p and len(p) >= 3 and bool_byte(p[2]) is not None:
        text = onoff(not bool_byte(p[2]))
        c = link.query([0xFA, 0x0C])  # SYSTEM_GET_EXT_PARAM: speak-to-chat config
        if c and len(c) >= 4 and c[2] in S2C_SENS and c[3] in S2C_TIMEOUT:
            text += f" (sensitivity {S2C_SENS[c[2]]}, timeout {S2C_TIMEOUT[c[3]]})"
        out.append(("Speak-to-Chat", text))

    p = link.query([0xF6, 0x01])  # SYSTEM_GET_PARAM: pause when taken off (byte = disabled)
    if p and len(p) >= 3 and bool_byte(p[2]) is not None:
        out.append(("Pause when removed", onoff(not bool_byte(p[2]))))

    p = link.query([0x26, 0x05])  # POWER_GET_PARAM: automatic power off
    if p and len(p) >= 4 and p[2] in (0x10, 0x11):
        text = onoff(p[2] == 0x10)
        if p[2] == 0x10 and p[3] in POWER_OFF_TIME and p[3] != 0x00:
            text += f" (after {POWER_OFF_TIME[p[3]]})"
        out.append(("Auto power off", text))

    p = link.query([0x46, 0x03], msg_type=MT_COMMAND_2)  # voice guidance (byte = disabled)
    if p and len(p) >= 3 and bool_byte(p[2]) is not None:
        out.append(("Voice guidance", onoff(not bool_byte(p[2]))))

    return out


def print_status(name, address, lines):
    lines = [("Device", f"{name} ({address})")] + lines
    width = max(len(label) for label, _ in lines) + 2
    for label, value in lines:
        print(f"{label + ':':<{width}}{value}")


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="Toggle noise cancelling / ambient sound on Sony headphones.")
    ap.add_argument("action", nargs="?",
                    choices=["toggle", "nc", "ambient", "off", "status"],
                    help="toggle: Noise Cancelling <-> Ambient Sound (Off -> NC; "
                         "Ambient <-> Off on devices without NC); "
                         "nc / ambient / off: set that mode; status: show battery, "
                         "mode and settings")
    ap.add_argument("--mac", help="Bluetooth address of the device "
                                  "(default: auto-detect the connected Sony device)")
    ap.add_argument("-v", "--verbose", action="store_true", help="dump frames to stderr")
    args = ap.parse_args()
    if args.action is None:
        ap.print_help()
        return
    VERBOSE = args.verbose

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    device_path, name, address = find_device(bus, args.mac)
    fd, unregister = open_rfcomm(bus, device_path)

    try:
        link = SonyLink(fd)
        asm, funcs = get_asm_type(link)
        st = get_state(link, asm)

        if args.action == "status":
            print_status(name, address, collect_status(link, asm, funcs, st))
            return

        has_nc = asm not in (ASM_SEAMLESS, ASM_ON_OFF)
        if args.action == "toggle":
            if has_nc:
                target = AMBIENT if st["mode"] == NC else NC
            else:
                target = OFF if st["mode"] == AMBIENT else AMBIENT
        else:
            target = {"nc": NC, "ambient": AMBIENT, "off": OFF}[args.action]

        if target == NC and not has_nc:
            raise SonyError("this device has no noise cancelling")

        if target != st["mode"]:
            set_state(link, asm, target, st)
            st = get_state(link, asm)

        print(MODE_LABEL[st["mode"]])
        if st["mode"] != target:
            raise SonyError(f"mode is {MODE_LABEL[st['mode']]}, expected {MODE_LABEL[target]}")
    finally:
        os.close(fd)
        unregister()


if __name__ == "__main__":
    try:
        main()
    except SonyError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
