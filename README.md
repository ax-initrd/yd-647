# YD-647 magstripe CLI

A Python terminal app for the Yumdatto/YD-647 tri-track (track 1/2/3)
magnetic stripe reader/encoder, talking to it over its USB-serial interface.
Reimplements every operation exposed by the vendor's Windows MFC demo app
(`Magnetic EncodeDlldemo`) directly over the serial protocol, without the
original DLL/EXE.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Find your device's serial port (macOS: something like
`/dev/tty.usbserial-XXXX`; Linux: `/dev/ttyUSB0`; Windows: `COMx`):

```bash
python yd647_cli.py list-ports
```

## Usage


```bash
python yd647_cli.py -i
# or skip the port prompt:
python yd647_cli.py -i --port /dev/tty.usbserial-XXXX
```

```bash
python yd647_cli.py --port /dev/tty.usbserial-XXXX ping
python yd647_cli.py --port /dev/tty.usbserial-XXXX status
python yd647_cli.py --port /dev/tty.usbserial-XXXX reset
python yd647_cli.py --port /dev/tty.usbserial-XXXX coercivity hi     # or lo
python yd647_cli.py --port /dev/tty.usbserial-XXXX density 210      # or 75

# read - swipe the card when prompted; each track is shown as both ASCII and hex
python yd647_cli.py --port /dev/tty.usbserial-XXXX read --tracks 1,2,3
python yd647_cli.py --port /dev/tty.usbserial-XXXX read --tracks 2 --output track2.json

# write - swipe the card when prompted
python yd647_cli.py --port /dev/tty.usbserial-XXXX write --coercivity hi --track1 "%B1234...?" --track2 "hex:3b313233..."
python yd647_cli.py --port /dev/tty.usbserial-XXXX write --input track2.json --yes

# erase (destructive - confirmation prompt unless --yes)
python yd647_cli.py --port /dev/tty.usbserial-XXXX erase --tracks 2
```

Any track combination (1, 2, 3, or any pair/triple) can be read, written, or
erased in a single pass; write/erase prompt for confirmation before touching
a physical card unless `--yes` is given.


## Track data: ASCII vs. hex

Every place track data enters or leaves this tool (`--track1`/`--track2`/
`--track3`, the interactive shell's prompts, and `read --output`/`write
--input` JSON files) uses the same convention:

- A `hex:` prefix means the rest is raw bytes as hex, e.g. `hex:1a2b3c` (
  spaces, colons and dashes inside the hex are ignored, so `hex:1a 2b 3c`
  and `hex:1a:2b:3c` both work too).
- An `ascii:` prefix, or no prefix at all, means the rest is literal ASCII
  text, taken exactly as given - including internal spaces and any
  shell-special characters, as long as they reach the program intact (see
  below).
- `read` always prints both forms for each track, e.g.:
  ```
  track 1 (62 bytes):
    ascii: 'ASCII DATA HERE'
    hex:   41 53 43 49 49 20 44 41 54 41 20 48 45 52 45
  ```
  This is what makes it possible to tell a genuine space (`0x20`, as in the
  padded name field above) apart from some other byte that merely displays
  as blank.
- `read --output file.json` always stores tracks as `hex:...` strings (for
  byte-for-byte fidelity), so `write --input file.json` round-trips a read
  exactly, and hand-written JSON files can use either `hex:...` or plain
  ASCII values.

