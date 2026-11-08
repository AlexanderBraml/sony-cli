# sony-cli

`sony-cli.py` switches Sony headphones and earbuds between Noise Cancelling
and Ambient Sound mode from the command line. The protocol was taken from
[BudsLink](https://github.com/maniacx/BudsLink) (Sony protocol v2, see
`src/lib/devices/sony/sonySocketV2.js`).

## Supported devices

Any Sony device that speaks Sony protocol **v2** should work. The script asks
the device which noise-cancelling / ambient-sound features it has, so nothing
is hardcoded per model. According to BudsLink this includes, among others:

- LinkBuds S
- WF-1000XM4, WF-1000XM5, WF-1000XM6
- WH-1000XM5, WH-1000XM6
- WF-C700N, WH-CH720N

> **Note:** it has mainly been tested on the **LinkBuds S**. Reports for other
> models are welcome.

Older models that only speak protocol v1 (e.g. WH-1000XM3, WH-1000XM4) are not
supported; the script tells you if it finds one.

## Requirements

- Linux with BlueZ, device paired **and connected**
- `/usr/bin/python3` with PyGObject (`sudo apt install python3-gi` on Debian/Ubuntu/Mint)
- BudsLink (or any other app that talks to the device's Sony service) must **not** be running

## Installation

`sony-cli.py` is a single standalone script with no dependencies beyond the
requirements above. You can run it straight from the repository, or copy it into
a `bin` directory on your `PATH` to call it from anywhere:

```
install -Dm755 sony-cli.py ~/.local/bin/sony-cli   # just for you
sudo install -m755 sony-cli.py /usr/local/bin/sony-cli   # system-wide
```

Make sure `~/.local/bin` is on your `PATH`. Then run e.g. `sony-cli toggle`.

## Usage

```
./sony-cli.py            # print help
./sony-cli.py toggle     # Noise Cancelling <-> Ambient Sound (Off -> NC)
./sony-cli.py nc         # Noise Cancelling
./sony-cli.py ambient    # Ambient Sound
./sony-cli.py off        # both off
./sony-cli.py status     # battery, codec, NC/ambient mode, EQ and other settings
./sony-cli.py --mac AA:BB:CC:DD:EE:FF toggle   # pick a specific device
./sony-cli.py -v status  # dump raw frames to stderr for debugging
```

The connected Sony device is detected automatically; if several are connected,
choose one with `--mac`. On devices without noise cancelling, `toggle` switches
between Ambient Sound and Off.

Focus-on-voice and the ambient level are kept as currently set on the device.
Bind `/path/to/sony-cli.py toggle` to a keyboard shortcut for one-key switching.

### Toggle and pause music

[`examples/sony-toggle-pause.sh`](examples/sony-toggle-pause.sh) toggles the
mode and, with the `pause` argument, pauses the music when switching to Ambient
Sound and resumes it when switching back to Noise Cancelling (via
[playerctl](https://github.com/altdesktop/playerctl)):

```
examples/sony-toggle-pause.sh          # only toggle, like `sony-cli toggle`
examples/sony-toggle-pause.sh pause    # toggle and pause / resume the music
PLAYER=vlc examples/sony-toggle-pause.sh pause   # control another player (default: spotify)
```

It uses `sony-cli` from your `PATH` if installed, otherwise `sony-cli.py` from
this repository. Handy as a keyboard shortcut for talking to someone.

### Fix: connected as hands-free only (mSBC / CVSD)

Sometimes the LinkBuds S connect with only the hands-free profile, so audio
is mono mSBC. The bluetoothd log then shows

```
a2dp-sink profile connect failed for XX:XX:...: Device or resource busy
```

The earbuds open the A2DP link at the same moment BlueZ does. Both attempts
collide, and BlueZ never retries. [`examples/bt-a2dp-fix.py`](examples/bt-a2dp-fix.py)
notices a connected audio device without A2DP and connects just the A2DP
profile again, which takes about a second and needs no disconnect. If that keeps
failing, it falls back to a full disconnect and reconnect. It works for any
Bluetooth headphones, not only Sony ones.

Run it in the background as a systemd user service:

```
install -Dm755 examples/bt-a2dp-fix.py ~/.local/bin/bt-a2dp-fix
install -Dm644 examples/bt-a2dp-fix.service ~/.config/systemd/user/bt-a2dp-fix.service
systemctl --user enable --now bt-a2dp-fix.service
journalctl --user -u bt-a2dp-fix -f      # see what it does
```

Or run it by hand (e.g. from a keyboard shortcut):

```
bt-a2dp-fix fix         # connect A2DP if missing
bt-a2dp-fix reconnect   # full disconnect + connect
```

`--mac AA:BB:CC:DD:EE:FF` limits it to one device.

## License

GPL-3.0-or-later (see `LICENSE`), as the protocol implementation is derived
from [BudsLink](https://github.com/maniacx/BudsLink), which is GPL-3.0.

## Disclaimer

This project is an independent effort and is not affiliated with, endorsed by,
or sponsored by Sony. "Sony" and "LinkBuds" are trademarks of their respective
holders and are used for identification purposes only.
