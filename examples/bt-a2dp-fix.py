#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Repair Bluetooth headphones that connected with only HFP/HSP (no A2DP).

Some devices (e.g. the Sony LinkBuds S) open the A2DP link themselves at the
same moment BlueZ does. The two attempts collide, BlueZ logs
"a2dp-sink profile connect failed ...: Device or resource busy" and never
retries, so only the hands-free profile is left (mono, mSBC/CVSD).

This script notices a connected audio device without an A2DP transport and
connects just the A2DP profile again (org.bluez.Device1.ConnectProfile). If
that keeps failing, it falls back to a full disconnect + connect.

Usage:
    bt-a2dp-fix.py watch     [--mac MAC]   run forever (systemd user service)
    bt-a2dp-fix.py fix       [--mac MAC]   check + repair connected devices once
    bt-a2dp-fix.py reconnect [--mac MAC]   force a full disconnect + connect

Requires: Python 3, PyGObject (python3-gi), BlueZ, PipeWire or PulseAudio.
"""

import argparse
import sys
import time

try:
    from gi.repository import Gio, GLib
except ImportError:
    sys.exit("error: PyGObject is missing (install python3-gi / python-gobject)")

A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"
A2DP_SINK = "0000110b-0000-1000-8000-00805f9b34fb"
DEVICE_IFACE = "org.bluez.Device1"
TRANSPORT_IFACE = "org.bluez.MediaTransport1"

CHECK_DELAY = 6          # s after a connect event before checking (let BlueZ finish)
SETTLE = 4               # s to wait after ConnectProfile before re-checking
PROFILE_ATTEMPTS = 3     # ConnectProfile attempts before a full reconnect
RECONNECT_COOLDOWN = 300  # s; at most one automatic full reconnect per device


def log(msg):
    print(msg, flush=True)


def call(bus, path, iface, method, args=None, timeout=-1):
    return bus.call_sync("org.bluez", path, iface, method, args, None,
                         Gio.DBusCallFlags.NONE, timeout, None)


def managed_objects(bus):
    reply = bus.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
                          "GetManagedObjects", None, None,
                          Gio.DBusCallFlags.NONE, -1, None)
    return reply.unpack()[0]


def device_props(bus, path):
    return managed_objects(bus).get(path, {}).get(DEVICE_IFACE)


def is_audio_sink(dev):
    return A2DP_SINK in [u.lower() for u in dev.get("UUIDs", [])]


def name_of(dev):
    return dev.get("Alias") or dev.get("Name") or dev.get("Address", "?")


def a2dp_up(bus, path):
    """True if BlueZ has an A2DP transport for this device.

    The transport carries the *local* role's UUID (we are the A2DP source),
    so accept both A2DP UUIDs; HFP/HSP transports are not BlueZ objects.
    """
    prefix = path + "/"
    for obj, ifaces in managed_objects(bus).items():
        t = ifaces.get(TRANSPORT_IFACE)
        if obj.startswith(prefix) and t and \
                t.get("UUID", "").lower() in (A2DP_SOURCE, A2DP_SINK):
            return True
    return False


def audio_devices(bus, mac=None, connected=True):
    """[(path, props)] of audio sink devices, optionally filtered by MAC."""
    out = []
    for path, ifaces in managed_objects(bus).items():
        dev = ifaces.get(DEVICE_IFACE)
        if not dev or not is_audio_sink(dev):
            continue
        if mac and dev.get("Address", "").upper() != mac.upper():
            continue
        if connected and not dev.get("Connected"):
            continue
        out.append((path, dev))
    return out


def wait_for(pred, timeout, step=0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def connected(bus, path):
    dev = device_props(bus, path)
    return bool(dev and dev.get("Connected"))


def connect_a2dp(bus, path):
    """One ConnectProfile attempt; True if A2DP is (still) up afterwards."""
    try:
        call(bus, path, DEVICE_IFACE, "ConnectProfile",
             GLib.Variant("(s)", (A2DP_SINK,)), timeout=15000)
    except GLib.Error as e:
        log(f"  ConnectProfile: {Gio.DBusError.get_remote_error(e) or e.message}")
    # The device sometimes drops A2DP again right away, so check it stays up.
    time.sleep(SETTLE)
    return a2dp_up(bus, path)


def full_reconnect(bus, path):
    name = name_of(device_props(bus, path) or {})
    log(f"{name}: disconnecting")
    try:
        call(bus, path, DEVICE_IFACE, "Disconnect", timeout=15000)
    except GLib.Error as e:
        log(f"  Disconnect: {e.message}")
    if not wait_for(lambda: not connected(bus, path), 15):
        log(f"{name}: did not disconnect")
        return False
    time.sleep(2)
    log(f"{name}: connecting")
    try:
        call(bus, path, DEVICE_IFACE, "Connect", timeout=30000)
    except GLib.Error as e:
        log(f"  Connect: {e.message}")
    ok = wait_for(lambda: a2dp_up(bus, path), 10)
    log(f"{name}: {'A2DP connected' if ok else 'still no A2DP'}")
    return ok


def repair(bus, path, allow_reconnect=True):
    """Make sure a connected audio device has A2DP. Returns True on success."""
    dev = device_props(bus, path)
    if not dev or not dev.get("Connected") or not is_audio_sink(dev):
        return True
    name = name_of(dev)
    if a2dp_up(bus, path):
        return True
    log(f"{name}: connected without A2DP, connecting A2DP profile")
    for attempt in range(1, PROFILE_ATTEMPTS + 1):
        if not connected(bus, path):
            log(f"{name}: disconnected meanwhile, giving up")
            return False
        if connect_a2dp(bus, path):
            log(f"{name}: A2DP connected (attempt {attempt})")
            return True
        log(f"{name}: attempt {attempt} failed")
    if allow_reconnect:
        return full_reconnect(bus, path)
    return False


# --- watch mode ---------------------------------------------------------------

class Watcher:
    def __init__(self, bus, mac):
        self.bus = bus
        self.mac = mac.upper() if mac else None
        self.pending = {}         # device path -> GLib timeout id
        self.last_reconnect = {}  # device path -> monotonic time

    def wanted(self, path):
        dev = device_props(self.bus, path)
        if not dev or not is_audio_sink(dev):
            return False
        return not self.mac or dev.get("Address", "").upper() == self.mac

    def schedule(self, path, reason):
        """(Re)start the delayed check for a device, debouncing bursts of events."""
        if path in self.pending:
            GLib.source_remove(self.pending[path])
        log(f"{path}: {reason}, checking in {CHECK_DELAY}s")
        self.pending[path] = GLib.timeout_add_seconds(CHECK_DELAY, self.check, path)

    def check(self, path):
        self.pending.pop(path, None)
        try:
            if not self.wanted(path):
                return GLib.SOURCE_REMOVE
            now = time.monotonic()
            allow = now - self.last_reconnect.get(path, -RECONNECT_COOLDOWN) >= RECONNECT_COOLDOWN
            if not a2dp_up(self.bus, path) and connected(self.bus, path):
                ok = repair(self.bus, path, allow_reconnect=False)
                if not ok and allow and connected(self.bus, path):
                    self.last_reconnect[path] = now
                    full_reconnect(self.bus, path)
                elif not ok:
                    log(f"{path}: giving up (full reconnect done recently)")
        except GLib.Error as e:
            log(f"{path}: D-Bus error: {e.message}")
        return GLib.SOURCE_REMOVE

    def on_props(self, _conn, _sender, path, _iface, _signal, params):
        iface, changed, _ = params.unpack()
        if iface != DEVICE_IFACE:
            return
        if changed.get("Connected") is True or changed.get("ServicesResolved") is True:
            if self.wanted(path):
                self.schedule(path, "connected")
        elif changed.get("Connected") is False and path in self.pending:
            GLib.source_remove(self.pending.pop(path))

    def on_removed(self, _conn, _sender, _path, _iface, _signal, params):
        obj, ifaces = params.unpack()
        if TRANSPORT_IFACE not in ifaces:
            return
        # /org/bluez/hciN/dev_XX_XX/sepN/fdN -> /org/bluez/hciN/dev_XX_XX
        dev_path = "/".join(obj.split("/")[:5])
        if dev_path.rsplit("/", 1)[-1].startswith("dev_") and self.wanted(dev_path) \
                and connected(self.bus, dev_path):
            self.schedule(dev_path, "A2DP transport removed while connected")

    def run(self):
        self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.Properties",
                                  "PropertiesChanged", None, None,
                                  Gio.DBusSignalFlags.NONE, self.on_props)
        self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.ObjectManager",
                                  "InterfacesRemoved", "/", None,
                                  Gio.DBusSignalFlags.NONE, self.on_removed)
        try:
            for path, _ in audio_devices(self.bus, self.mac):
                self.schedule(path, "already connected at startup")
        except GLib.Error as e:
            log(f"BlueZ not available yet: {e.message}")
        log("watching for audio devices connecting without A2DP")
        GLib.MainLoop().run()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["watch", "fix", "reconnect"])
    ap.add_argument("--mac", help="only handle this device (AA:BB:CC:DD:EE:FF)")
    args = ap.parse_args()

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    if args.command == "watch":
        try:
            Watcher(bus, args.mac).run()
        except KeyboardInterrupt:
            pass
        return 0

    devices = audio_devices(bus, args.mac)
    if not devices:
        what = args.mac or "Bluetooth audio device"
        sys.exit(f"error: no connected {what} found")

    ok = True
    for path, dev in devices:
        if args.command == "reconnect":
            ok &= full_reconnect(bus, path)
        elif a2dp_up(bus, path):
            log(f"{name_of(dev)}: A2DP already connected")
        else:
            ok &= repair(bus, path)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
