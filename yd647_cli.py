#!/usr/bin/env python3
"""Terminal app for the Yumdatto YD-647 tri-track magnetic stripe
reader/encoder, talking to the device over its USB-serial interface."""
from __future__ import annotations

import argparse
import json
import re
import sys

import serial
from serial.tools import list_ports

from yd647 import YD647, MSRError, TRACK_MAX_LEN, TRACK2_MAX_LEN_75BPI, TRACK23_CHARSET


def _parse_tracks(spec: str | None) -> set[int]:
    if not spec:
        return {1, 2, 3}
    tracks = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid track number: {part!r}")
        if n not in (1, 2, 3):
            raise argparse.ArgumentTypeError(f"track must be 1, 2 or 3, got {n}")
        tracks.add(n)
    if not tracks:
        raise argparse.ArgumentTypeError("no tracks given")
    return tracks


# ---------------------------------------------------------------------------
# track data input/output: every place track data enters or leaves this tool
# (CLI flags, the interactive shell, JSON files) uses the same 'hex:'/'ascii:'
# string convention, so a track can be given as either raw hex bytes or
# literal ASCII text.
# ---------------------------------------------------------------------------

_HEX_CLEAN_RE = re.compile(r"[\s:,-]+")


def parse_track_input(spec: str, track: int | None = None) -> bytes:
    """Parse user-supplied track data. A 'hex:' prefix means the rest is a
    hex string (whitespace/':'/'-'/',' separators are ignored, so
    "hex:1a 2b 3c" and "hex:1a:2b:3c" both work); an 'ascii:' prefix or no
    prefix at all means the rest is literal text, encoded as ASCII bytes
    exactly as given (spaces and all - this function never splits or
    otherwise interprets the string, so it is safe to pass data containing
    spaces or shell-special characters as long as it arrives here intact,
    e.g. via a quoted CLI argument, the interactive prompt, or a JSON file).

    When `track` is 2 or 3 and plain ASCII text was given (not 'hex:'), the
    result is checked against the restricted ABA character set those tracks
    actually use (digits and a handful of punctuation - no letters at all).
    Confirmed on real hardware: a letter there gets rejected outright by the
    device rather than it ever waiting for a swipe, which otherwise shows up
    as a confusing "no read/write in progress" error with no obvious cause.
    This check is skipped for 'hex:' input, since sending arbitrary/invalid
    bytes on purpose (e.g. to see how the device reacts) is a legitimate use
    of this tool."""
    if spec.startswith("hex:"):
        cleaned = _HEX_CLEAN_RE.sub("", spec[4:])
        try:
            return bytes.fromhex(cleaned)
        except ValueError as e:
            raise ValueError(f"invalid hex track data {spec[4:]!r}: {e}") from e
    if spec.startswith("ascii:"):
        spec = spec[6:]
    try:
        data = spec.encode("ascii")
    except UnicodeEncodeError as e:
        raise ValueError(f"track data must be ASCII, or use a 'hex:' prefix for raw bytes: {e}") from e
    if track in (2, 3):
        bad = sorted({chr(b) for b in data if b not in TRACK23_CHARSET})
        if bad:
            punctuation = sorted(chr(c) for c in TRACK23_CHARSET if not chr(c).isdigit())
            raise ValueError(
                f"track {track} data contains invalid character(s) {', '.join(bad)} - "
                f"track {track} only supports digits 0-9 and {' '.join(punctuation)} (no letters). "
                "Only track 1 supports full text; use a 'hex:' prefix instead if you really want to send this"
            )
    return data


def format_track(data: bytes) -> str:
    ascii_repr = data.decode("ascii", errors="replace")
    hex_repr = data.hex(" ") if data else ""
    return f"ascii: {ascii_repr!r}\n  hex:   {hex_repr}"


def _warn_track2_length(tr2: bytes | None) -> None:
    if tr2 is not None and len(tr2) > TRACK2_MAX_LEN_75BPI:
        print(
            f"warning: track 2 data is {len(tr2)} bytes (>{TRACK2_MAX_LEN_75BPI}); "
            "this requires the device to be set to 210 BPI (`density 210`) first",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_list_ports(args) -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("no serial ports found")
        return
    for p in ports:
        print(f"{p.device}\t{p.description}")


def _select_port_interactive() -> str:
    """List detected serial ports with a number next to each and prompt for
    that number, rather than requiring the user to type/paste the device
    path."""
    ports = list(list_ports.comports())
    if not ports:
        print("no serial ports found - plug in the device and try again", file=sys.stderr)
        sys.exit(1)
    print("available serial ports:")
    for i, p in enumerate(ports, 1):
        print(f"  {i}) {p.device}\t{p.description}")
    while True:
        raw = input(f"select port [1-{len(ports)}]: ").strip()
        try:
            idx = int(raw)
        except ValueError:
            print(f"  enter a number from 1 to {len(ports)}")
            continue
        if 1 <= idx <= len(ports):
            return ports[idx - 1].device
        print(f"  enter a number from 1 to {len(ports)}")


def cmd_ping(args) -> None:
    with YD647(args.port, args.baud, args.timeout) as dev:
        ok = dev.ping()
    print("device responded OK" if ok else "no response / unexpected reply")
    sys.exit(0 if ok else 1)


def cmd_status(args) -> None:
    with YD647(args.port, args.baud, args.timeout) as dev:
        ok = dev.status()
    print("last operation: SUCCESS" if ok else "last operation: FAILED")


def cmd_reset(args) -> None:
    with YD647(args.port, args.baud, args.timeout) as dev:
        dev.reset()
    print("reset command sent")


def cmd_coercivity(args) -> None:
    with YD647(args.port, args.baud, args.timeout) as dev:
        dev.set_coercivity(args.mode == "hi")
    print(f"coercivity set to {args.mode.upper()}Co")


def cmd_density(args) -> None:
    with YD647(args.port, args.baud, args.timeout) as dev:
        dev.set_track2_density(args.mode == "210")
    print(f"track 2 density set to {args.mode} BPI")


def _print_read_results(tracks_requested, data: dict[int, bytes]) -> None:
    for t in sorted(tracks_requested):
        if t in data:
            print(f"track {t} ({len(data[t])} bytes):")
            print(f"  {format_track(data[t])}")
        else:
            print(f"track {t}: no data (track is empty or unreadable on this card)")


def cmd_read(args) -> None:
    tracks = _parse_tracks(args.tracks)
    with YD647(args.port, args.baud, args.timeout) as dev:
        print("swipe card now...")
        data = dev.read_tracks(tracks)
    _print_read_results(tracks, data)
    if args.output:
        # stored as 'hex:...' strings so `write --input` can read the file
        # straight back, byte-for-byte, without any ASCII round-trip risk
        with open(args.output, "w") as f:
            json.dump({str(k): f"hex:{v.hex()}" for k, v in data.items()}, f, indent=2)
        print(f"saved to {args.output}")


def cmd_write(args) -> None:
    tr_spec: dict[int, str | None] = {1: args.track1, 2: args.track2, 3: args.track3}
    if args.input:
        with open(args.input) as f:
            loaded = json.load(f)
        for k, v in loaded.items():
            tr_spec[int(k)] = v

    if not any(v is not None for v in tr_spec.values()):
        print("error: nothing to write - provide --track1/--track2/--track3 or --input", file=sys.stderr)
        sys.exit(2)

    try:
        tr: dict[int, bytes | None] = {
            t: (parse_track_input(v, track=t) if v is not None else None) for t, v in tr_spec.items()
        }
        for t, v in tr.items():
            if v is not None and len(v) > TRACK_MAX_LEN[t]:
                raise ValueError(f"track {t} data is {len(v)} bytes, max is {TRACK_MAX_LEN[t]}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    _warn_track2_length(tr[2])

    if not args.yes:
        chosen = ", ".join(f"track {t}" for t, v in tr.items() if v is not None)
        resp = input(f"about to write {chosen} to a physical card. Continue? [y/N] ")
        if resp.strip().lower() != "y":
            print("aborted")
            return

    with YD647(args.port, args.baud, args.timeout) as dev:
        if args.coercivity:
            dev.set_coercivity(args.coercivity == "hi")
            print(f"coercivity set to {args.coercivity.upper()}Co")
        print("swipe card now...")
        dev.write_tracks(tr1=tr[1], tr2=tr[2], tr3=tr[3])
    print("write OK")


def cmd_erase(args) -> None:
    tracks = _parse_tracks(args.tracks)
    if not args.yes:
        chosen = ", ".join(f"track {t}" for t in sorted(tracks))
        resp = input(f"about to ERASE {chosen} on a physical card - this cannot be undone. Continue? [y/N] ")
        if resp.strip().lower() != "y":
            print("aborted")
            return
    with YD647(args.port, args.baud, args.timeout) as dev:
        print("swipe card now...")
        dev.erase_tracks(tracks)
    print("erase OK")


# ---------------------------------------------------------------------------
# interactive shell
# ---------------------------------------------------------------------------

MENU = """
 1) Read card
 2) Write card
 3) Erase card
 4) Set coercivity (Hi-Co / Lo-Co)
 5) Set track 2 density (75 / 210 BPI)
 6) Reset device
 7) Ping / connect check
 8) Read/write status of last operation
 9) Quit
"""


def _prompt_tracks(action: str) -> set[int]:
    while True:
        raw = input(f"  which tracks to {action}? (e.g. 1,2,3) [1,2,3] ").strip()
        try:
            return _parse_tracks(raw)
        except argparse.ArgumentTypeError as e:
            print(f"  {e}")


def _run_with_repeat(action) -> None:
    """Run `action()` once, then offer to repeat the exact same action (same
    tracks/data/mode, just prompting for another swipe) or return to the
    menu - lets a user re-read/re-write/erase a stack of cards without
    re-entering the same parameters each time."""
    while True:
        try:
            action()
        except MSRError as e:
            print(f"  error: {e}")
        except serial.SerialException as e:
            print(f"  serial error: {e}")
        again = input("  [r]epeat this action, or [e]xit to menu? ").strip().lower()
        if again != "r":
            return


def cmd_shell(args) -> None:
    print(f"YD-647 magstripe encoder - connecting to {args.port} @ {args.baud} baud")
    dev = YD647(args.port, args.baud, args.timeout)
    try:
        dev.open()
    except serial.SerialException as e:
        print(f"failed to open port: {e}")
        return
    print("connected.")
    try:
        while True:
            print(MENU)
            choice = input("choose> ").strip()
            action = None
            try:
                if choice == "1":
                    tracks = _prompt_tracks("read")

                    def action(tracks=tracks):
                        print("  swipe card now...")
                        data = dev.read_tracks(tracks)
                        for t in sorted(tracks):
                            if t in data:
                                print(f"  track {t} ({len(data[t])} bytes): {format_track(data[t])}")
                            else:
                                print(f"  track {t}: no data (track is empty or unreadable on this card)")

                elif choice == "2":
                    tracks = _prompt_tracks("write")
                    tr: dict[int, bytes] = {}
                    for t in sorted(tracks):
                        raw = input(f"  track {t} data (plain text, or 'hex:...' for raw bytes): ")
                        try:
                            tr[t] = parse_track_input(raw, track=t)
                        except ValueError as e:
                            print(f"  {e}")
                            break
                    else:
                        _warn_track2_length(tr.get(2))

                        def action(tr=tr):
                            print("  swipe card now...")
                            dev.write_tracks(tr1=tr.get(1), tr2=tr.get(2), tr3=tr.get(3))
                            print("  write OK")

                elif choice == "3":
                    tracks = _prompt_tracks("erase")
                    confirm = input(f"  ERASE tracks {sorted(tracks)} - this cannot be undone. Continue? [y/N] ")
                    if confirm.strip().lower() == "y":

                        def action(tracks=tracks):
                            print("  swipe card now...")
                            dev.erase_tracks(tracks)
                            print("  erase OK")

                    else:
                        print("  aborted")
                elif choice == "4":
                    m = input("  hi or lo? ").strip().lower()
                    if m not in ("hi", "lo"):
                        print("  enter 'hi' or 'lo'")
                        continue

                    def action(m=m):
                        dev.set_coercivity(m == "hi")
                        print(f"  coercivity set to {m}")

                elif choice == "5":
                    m = input("  75 or 210? ").strip()
                    if m not in ("75", "210"):
                        print("  enter '75' or '210'")
                        continue

                    def action(m=m):
                        dev.set_track2_density(m == "210")
                        print(f"  track 2 density set to {m} BPI")

                elif choice == "6":

                    def action():
                        dev.reset()
                        print("  reset sent")

                elif choice == "7":

                    def action():
                        ok = dev.ping()
                        print("  device OK" if ok else "  no response")

                elif choice == "8":

                    def action():
                        ok = dev.status()
                        print("  last op: SUCCESS" if ok else "  last op: FAILED")

                elif choice == "9":
                    break
                else:
                    print("  unknown choice")
            except MSRError as e:
                print(f"  error: {e}")
                continue
            except serial.SerialException as e:
                print(f"  serial error: {e}")
                continue

            if action is not None:
                _run_with_repeat(action)
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        dev.close()
        print("port closed.")


# ---------------------------------------------------------------------------
# argument parsing / dispatch
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CLI/terminal app for the YD-647 magstripe reader/encoder")
    p.add_argument(
        "-i", "--interactive", action="store_true", help="launch the interactive menu-driven session"
    )
    p.add_argument("--port", help="serial device, e.g. /dev/tty.usbserial-XXXX (macOS/Linux) or COM3 (Windows)")
    p.add_argument("--baud", type=int, default=9600, help="baud rate (default: 9600)")
    p.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help=(
            "max seconds to wait for a card swipe (default: 20.0, matching the device's own "
            "auto-reset window). Results are returned as soon as the device responds, not "
            "after this elapses - it's just an upper bound for how long you have to swipe"
        ),
    )
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list-ports", help="list available serial ports")
    sub.add_parser("ping", help="handshake / connect-to-device check")
    sub.add_parser("status", help="query the outcome of the last read/write")
    sub.add_parser("reset", help="reset the device")

    pc = sub.add_parser("coercivity", help="set Hi-Co / Lo-Co write mode")
    pc.add_argument("mode", choices=["hi", "lo"])

    pd = sub.add_parser("density", help="set track 2 recording density")
    pd.add_argument("mode", choices=["75", "210"])

    pr = sub.add_parser("read", help="read one or more track [--track 1,2,3 | DEF: all]")
    pr.add_argument("--tracks", help="comma-separated list from {1,2,3} (default: all)")
    pr.add_argument("--output", help="save the result as JSON to this file")

    pw = sub.add_parser("write", help="write one or more tracks [--track1/--track2/--track3]")
    track_help = "literal ASCII text (quote it if it has spaces), or 'hex:...' for raw bytes, e.g. 'hex:1a2b3c'"
    pw.add_argument("--track1", help=track_help)
    pw.add_argument("--track2", help=track_help)
    pw.add_argument("--track3", help=track_help)
    pw.add_argument("--input", help="JSON file with track data (as produced by `read --output`)")
    pw.add_argument(
        "--coercivity",
        choices=["hi", "lo"],
        help=(
            "set Hi-Co/Lo-Co before writing (must match the card's actual stripe type or the "
            "write will physically fail/scramble the track - easy to forget since it's a "
            "separate command otherwise)"
        ),
    )
    pw.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    pe = sub.add_parser("erase", help="erase one or more tracks [--track 1,2,3 | DEF: all]")
    pe.add_argument("--tracks", help="comma-separated list from {1,2,3} (default: all)")
    pe.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    sub.add_parser("shell", help="interactive menu-driven mode (same as -i)")
    return p


DISPATCH = {
    "list-ports": cmd_list_ports,
    "ping": cmd_ping,
    "status": cmd_status,
    "reset": cmd_reset,
    "coercivity": cmd_coercivity,
    "density": cmd_density,
    "read": cmd_read,
    "write": cmd_write,
    "erase": cmd_erase,
    "shell": cmd_shell,
}


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.interactive:
        args.command = "shell"

    if not args.command:
        # No subcommand and no -i: don't guess, just show usage.
        parser.print_help()
        return

    if args.command == "list-ports":
        cmd_list_ports(args)
        return

    if not args.port:
        if args.command == "shell":
            args.port = _select_port_interactive()
        else:
            parser.error("--port is required (see `list-ports` to discover it)")

    try:
        DISPATCH[args.command](args)
    except MSRError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except serial.SerialException as e:
        print(f"serial error: {e}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
