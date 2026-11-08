# linkbuds-cli

`linkbuds.py` switches a Sony **LinkBuds S** between Noise Cancelling and
Ambient Sound mode from the command line. The protocol was taken from
[BudsLink](https://github.com/maniacx/BudsLink) (Sony protocol v2, see
`src/lib/devices/sony/sonySocketV2.js`).

## Requirements

- Linux with BlueZ, buds paired **and connected**
- `/usr/bin/python3` with PyGObject (`sudo apt install python3-gi` on Debian/Ubuntu/Mint)
- BudsLink (or any other app that talks to the buds' Sony service) must **not** be running

## Usage

```
./linkbuds.py            # toggle: Noise Cancelling <-> Ambient Sound (Off -> NC)
./linkbuds.py nc         # Noise Cancelling
./linkbuds.py ambient    # Ambient Sound
./linkbuds.py off        # both off
./linkbuds.py status     # print current mode
./linkbuds.py --mac AA:BB:CC:DD:EE:FF toggle   # pick a specific device
./linkbuds.py -v status  # dump raw frames to stderr for debugging
```

Focus-on-voice and the ambient level are kept as currently set on the buds.
Bind `/path/to/linkbuds.py toggle` to a keyboard shortcut for one-key switching.
