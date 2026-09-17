"""Driver for the Yumdatto YD-647 tri-track magnetic stripe reader/encoder.

Physical link: USB-to-RS232 adapter presenting a standard serial port
(8N1, 9600 bps by default).
"""
from __future__ import annotations

import time

import serial

ESC = 0x1B

# ---------------------------------------------------------------------------
# Track-combination bookkeeping
# ---------------------------------------------------------------------------

TRACK_COMBOS = {
    frozenset({1}): 1,
    frozenset({2}): 2,
    frozenset({1, 2}): 3,
    frozenset({3}): 4,
    frozenset({1, 3}): 5,
    frozenset({2, 3}): 6,
    frozenset({1, 2, 3}): 7,
}

# Read command bytes per combo (manual section 7). Combo 5 (track 1+3) has
# no single-shot command in either the manual or the firmware
READ_COMMANDS = {
    1: bytes((ESC, 0x42, 0x4D, ESC, 0x6A)),  # ESC B M ESC j
    2: bytes((ESC, 0x5D, ESC, 0x6A)),        # ESC ]   ESC j
    3: bytes((ESC, 0x42, 0x54, ESC, 0x6A)),  # ESC B T ESC j
    4: bytes((ESC, 0x54, 0x5D, ESC, 0x6A)),  # ESC T ] ESC j
    6: bytes((ESC, 0x42, 0x5D, ESC, 0x6A)),  # ESC B ] ESC j
    7: bytes((ESC, 0x42, 0x74, ESC, 0x6A)),  # ESC B t ESC j
}

# Erase byte values, per manual section 7 ("Erase" Command table)
ERASE_CODES = {
    frozenset({1}): 0x00,
    frozenset({2}): 0x02,
    frozenset({3}): 0x03,
    frozenset({1, 2}): 0x04,
    frozenset({1, 3}): 0x05,
    frozenset({2, 3}): 0x06,
    frozenset({1, 2, 3}): 0x07,
}

HICO = 0x78
LOCO = 0x79

# Track-2 density modes (manual section 6): "ESC H" -> 210 BPI, "ESC L" -> 75 BPI.
DENSITY_210BPI = 0x48  # 'H'
DENSITY_75BPI = 0x4C   # 'L'

TRACK_MAX_LEN = {1: 76, 2: 104, 3: 104}
TRACK2_MAX_LEN_75BPI = 37

TRACK23_CHARSET = frozenset(b"0123456789:;<=>#@'")

FAILED_TRACK_MARKER = b"\x7f"


class MSRError(Exception):
    """Raised when the device reports a failed operation or sends an
    unexpected/unparseable response."""


class YD647:
    """Serial driver for the YD-647 magstripe reader/encoder."""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 20.0,
        idle_gap: float = 0.3,
        poll_interval: float = 0.05,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.idle_gap = idle_gap
        self.poll_interval = poll_interval
        self._ser: serial.Serial | None = None

    # -- connection management -------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self) -> None:
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.poll_interval,
            write_timeout=2.0,
        )
        try:
            self._ser.dtr = True
        except (OSError, ValueError):
            pass
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.dtr = False
            except (OSError, ValueError):
                pass
            self._ser.close()
            self._ser = None

    def __enter__(self) -> "YD647":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- low-level I/O -----------------------------------------------------

    def _require_open(self) -> None:
        if not self.is_open:
            raise MSRError("serial port is not open")

    def _write(self, data: bytes) -> None:
        self._require_open()
        self._ser.reset_input_buffer()
        self._ser.write(data)
        self._ser.flush()

    def _read(self, max_size: int = 1024, overall_timeout: float | None = None) -> bytes:
        """Read a response, waiting up to `overall_timeout` (defaults to
        self.timeout) for the device to start replying at all - e.g. the
        time given for the user to actually swipe a card - but returning as
        soon as the device goes quiet for self.idle_gap seconds after that,
        rather than always blocking for the full timeout regardless of how
        quickly the device actually responded."""
        self._require_open()
        if overall_timeout is None:
            overall_timeout = self.timeout
        deadline = time.monotonic() + overall_timeout
        buf = b""
        last_byte_at = None
        while len(buf) < max_size:
            chunk = self._ser.read(max_size - len(buf))
            now = time.monotonic()
            if chunk:
                buf += chunk
                last_byte_at = now
                continue
            if buf and (now - last_byte_at) >= self.idle_gap:
                break  # device has gone quiet - response is complete
            if now >= deadline:
                break  # nothing ever arrived (or stopped early) - give up
        return buf

    def _read_rpq(self, resp: bytes, context: str) -> bool:
        """Parse the generic '...r p/q/r' status trailer used by write and
        status-query responses (manual section 7). Returns True on success
        ('p'), False on reported failure ('q'), and raises for 'r' (no
        read/write took place) or an unparseable response."""
        idx = resp.find(b"r")
        if idx == -1 or idx + 1 >= len(resp):
            raise MSRError(f"unexpected {context} response: {resp!r}")
        code = resp[idx + 1]
        if code == ord("p"):
            return True
        if code == ord("q"):
            return False
        raise MSRError(f"no read/write in progress (device response to {context}: {resp!r})")

    # -- device commands -----------------------------------------------------

    def ping(self) -> bool:
        """Handshake / "communication with device" check (ESC e). Expects a
        reply starting with ESC y."""
        self._write(bytes((ESC, 0x65)))
        resp = self._read(32)
        return len(resp) >= 2 and resp[0] == ESC and resp[1] == 0x79

    def reset(self) -> None:
        self._write(bytes((ESC, 0x30)))

    def set_coercivity(self, hico: bool) -> None:
        self._write(bytes((ESC, HICO if hico else LOCO)))

    def set_track2_density(self, high: bool) -> None:
        self._write(bytes((ESC, DENSITY_210BPI if high else DENSITY_75BPI)))

    def status(self) -> bool:
        """Query the outcome of the last read/write operation (ESC j)."""
        self._write(bytes((ESC, 0x6A)))
        resp = self._read(1024)
        return self._read_rpq(resp, "status query")

    def read_tracks(self, tracks) -> dict[int, bytes]:
        """Read the given tracks (any non-empty subset of {1, 2, 3}).
        Blocks until the card is swiped or the read times out. Returns the
        raw bytes exactly as received from the device for each track (no
        ASCII decoding/validation - track 1 uses the IATA/ASCII character
        set but tracks 2/3 use a restricted numeric encoding, and callers
        may want the exact bytes either way)."""
        tracks = frozenset(tracks)
        if not tracks or not tracks.issubset({1, 2, 3}):
            raise ValueError("tracks must be a non-empty subset of {1, 2, 3}")

        if tracks == frozenset({1, 3}):
            out: dict[int, bytes] = {}
            for t in (1, 3):
                try:
                    out.update(self.read_tracks((t,)))
                except MSRError:
                    pass
            if not out:
                raise MSRError(f"no data on requested track(s) {sorted(tracks)} - track(s) empty or unreadable")
            return out

        combo = TRACK_COMBOS[tracks]
        self._write(READ_COMMANDS[combo])
        resp = self._read(1024)
        if not resp:
            raise MSRError("no response from device - swipe not detected, or the read timed out")
        try:
            end = resp.index(b"?")
        except ValueError:
            raise MSRError(f"malformed/incomplete read response - swipe not detected in time? (response: {resp!r})")

        raw: dict[int, bytes] = {}
        if combo == 1:
            raw[1] = resp[3:end]
        elif combo == 2:
            raw[2] = resp[2:end]
        elif combo == 4:
            raw[3] = resp[3:end]
        elif combo == 3:
            sep = resp.index(b"B", 0, end)
            raw[2] = resp[2:sep]
            raw[1] = resp[sep + 1 : end]
        elif combo == 6:
            sep = resp.find(b"A", 0, end)
            if sep == -1:
                raw[2] = resp[2:end]
                raw[3] = b""
            else:
                raw[2] = resp[2:sep]
                raw[3] = resp[sep + 1 : end]
        elif combo == 7:
            pos_a = resp.find(b"A", 0, end)
            pos_b = resp.find(b"B", 0, end)
            candidates = [p for p in (pos_a, pos_b) if p != -1]
            if not candidates:
                raise MSRError(f"malformed read response (no track separators found): {resp!r}")
            sep1 = min(candidates)
            if resp[sep1] == ord("A"):
                sep2 = resp.index(b"B", sep1 + 1, end)
                raw[2] = resp[2:sep1]
                raw[3] = resp[sep1 + 1 : sep2]
                raw[1] = resp[sep2 + 1 : end]
            else:  # collapsed empty-track-3 case: single 'B' separator only
                raw[2] = resp[2:sep1]
                raw[3] = b""
                raw[1] = resp[sep1 + 1 : end]

        missing = {t for t, v in raw.items() if v == FAILED_TRACK_MARKER or v == b""}
        for t in missing:
            del raw[t]
        if not raw:
            raise MSRError(
                f"no data on requested track(s) {sorted(tracks)} - track empty or unreadable "
                f"(response: {resp!r})"
            )
        return raw

    def write_tracks(self, tr1: bytes | None = None, tr2: bytes | None = None, tr3: bytes | None = None) -> None:
        """Write the given tracks (any combination of tr1/tr2/tr3 may be
        None to leave that track alone). tr1/tr2/tr3 are raw bytes to write
        verbatim - the caller is responsible for any ASCII/hex decoding of
        user input before calling this. Blocks until the card is swiped or
        the write times out."""
        tracks = {t for t, v in ((1, tr1), (2, tr2), (3, tr3)) if v is not None}
        if not tracks:
            raise ValueError("at least one of tr1/tr2/tr3 must be provided")

        for t, v in ((1, tr1), (2, tr2), (3, tr3)):
            if v is not None and len(v) > TRACK_MAX_LEN[t]:
                raise ValueError(f"track {t} data exceeds {TRACK_MAX_LEN[t]} bytes")

        b1 = tr1 or b""
        b2 = tr2 or b""
        b3 = tr3 or b""

        combo = TRACK_COMBOS[frozenset(tracks)]
        escb = bytes((ESC,))
        tail = bytes((0x1D, ESC, 0x5C))  # GS ESC \
        if combo == 1:
            body = escb + b"t" + b"B" + b1
        elif combo == 2:
            body = escb + b"t" + b2
        elif combo == 3:
            body = escb + b"t" + b2 + b"B" + b1
        elif combo == 4:
            body = escb + b"t" + b"A" + b3
        elif combo == 5:
            body = escb + b"t" + b"A" + b3 + b"B" + b1
        elif combo == 6:
            body = escb + b"t" + b2 + b"A" + b3
        else:  # combo == 7
            body = escb + b"t" + b2 + b"A" + b3 + b"B" + b1

        cmd = body + tail + escb + b"j"
        self._write(cmd)
        resp = self._read(1024)
        ok = self._read_rpq(resp, "write")
        if not ok:
            hint = (
                "possible causes: coercivity (Hi-Co/Lo-Co) doesn't match this card - set it "
                "with the coercivity command before writing; an inconsistent/too-fast-or-slow "
                "swipe; or track 2 data too long for the current density setting. "
            )
            if len(tracks) > 1:
                hint += (
                    "Note: this was a multi-track write, and the device only reports one "
                    "overall pass/fail with no per-track detail - some of the requested "
                    "tracks may have actually written correctly. Read the card back to check, "
                    "or write each track individually for an unambiguous per-track result."
                )
            raise MSRError(f"write failed (device reported failure). {hint}")

    def erase_tracks(self, tracks) -> None:
        """Erase the given tracks (any non-empty subset of {1, 2, 3})."""
        tracks = frozenset(tracks)
        if not tracks or not tracks.issubset({1, 2, 3}):
            raise ValueError("tracks must be a non-empty subset of {1, 2, 3}")
        code = ERASE_CODES[tracks]
        self._write(bytes((ESC, 0x63, code)))
        resp = self._read(32)
        if len(resp) >= 2 and resp[0] == ESC and resp[1] == 0x30:
            return
        raise MSRError(f"erase failed (response: {resp!r})")
