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

## License

GPL-3.0-or-later (see `LICENSE`), as the protocol implementation is derived
from [BudsLink](https://github.com/maniacx/BudsLink), which is GPL-3.0.

## Disclaimer

This project is an independent effort and is not affiliated with, endorsed by,
or sponsored by Sony. "Sony" and "LinkBuds" are trademarks of their respective
holders and are used for identification purposes only.
