# sony-cli

`sony-cli.py` switches a Sony **LinkBuds S** between Noise Cancelling and
Ambient Sound mode from the command line. The protocol was taken from
[BudsLink](https://github.com/maniacx/BudsLink) (Sony protocol v2, see
`src/lib/devices/sony/sonySocketV2.js`).

## Requirements

- Linux with BlueZ, buds paired **and connected**
- `/usr/bin/python3` with PyGObject (`sudo apt install python3-gi` on Debian/Ubuntu/Mint)
- BudsLink (or any other app that talks to the buds' Sony service) must **not** be running

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

Focus-on-voice and the ambient level are kept as currently set on the buds.
Bind `/path/to/sony-cli.py toggle` to a keyboard shortcut for one-key switching.

## License

GPL-3.0-or-later (see `LICENSE`), as the protocol implementation is derived
from [BudsLink](https://github.com/maniacx/BudsLink), which is GPL-3.0.

## Disclaimer

This project is an independent effort and is not affiliated with, endorsed by,
or sponsored by Sony. "Sony" and "LinkBuds" are trademarks of their respective
holders and are used for identification purposes only.
