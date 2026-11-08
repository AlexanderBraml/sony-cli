#!/usr/bin/python3
"""Switch a Sony LinkBuds S between Noise Cancelling and Ambient Sound mode.

Protocol extracted from BudsLink (src/lib/devices/sony/sonySocketBase.js,
sonySocketV2.js, sonyConfig.js, profileManager.js). The LinkBuds S speaks
Sony protocol "v2" over an RFCOMM channel that is opened through BlueZ's
Profile API, exactly like BudsLink does it.

Usage:
    linkbuds.py [toggle|nc|ambient|off|status] [--mac AA:BB:CC:DD:EE:FF] [-v]

Requires: Python 3, PyGObject (python3-gi), BlueZ, buds paired + connected.
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

SONY_UUID_V2 = "956c7b26-d49a-4ba8-b03f-b17d393cb6e2"
NAME_PATTERN = "LinkBuds S"
PROFILE_PATH = "/io/github/linkbudscli/profile"

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

    candidates = []
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        address = dev.get("Address", "")
        name = dev.get("Name") or dev.get("Alias") or ""
        uuids = [u.lower() for u in dev.get("UUIDs", [])]
        if mac:
            if address.upper() == mac.upper():
                candidates = [(path, name, dev)]
                break
        elif SONY_UUID_V2 in uuids and NAME_PATTERN in name:
            candidates.append((path, name, dev))

    if not candidates:
        what = mac if mac else f"a paired '{NAME_PATTERN}'"
        raise SonyError(f"could not find {what} in BlueZ")

    connected = [c for c in candidates if c[2].get("Connected")]
    if not connected:
        raise SonyError(f"'{candidates[0][1]}' is paired but not connected")

    path, name, _ = connected[0]
    log(f"using device {name} ({path})")
    return path


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
            "Name": GLib.Variant("s", "linkbuds-cli"),
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
            return asm
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


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="Toggle Sony LinkBuds S noise cancelling / ambient sound.")
    ap.add_argument("action", nargs="?", default="toggle",
                    choices=["toggle", "nc", "ambient", "off", "status"])
    ap.add_argument("--mac", help="Bluetooth address of the buds (default: auto-detect)")
    ap.add_argument("-v", "--verbose", action="store_true", help="dump frames to stderr")
    args = ap.parse_args()
    VERBOSE = args.verbose

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    device_path = find_device(bus, args.mac)
    fd, unregister = open_rfcomm(bus, device_path)

    try:
        link = SonyLink(fd)
        asm = get_asm_type(link)
        st = get_state(link, asm)

        if args.action == "status":
            print(MODE_LABEL[st["mode"]])
            return

        if args.action == "toggle":
            target = NC if st["mode"] == AMBIENT else AMBIENT
        else:
            target = {"nc": NC, "ambient": AMBIENT, "off": OFF}[args.action]

        if target == NC and asm in (ASM_SEAMLESS, ASM_ON_OFF):
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
