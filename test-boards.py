#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""Hardware test of the SLCAN bridge with two boards on one CAN bus.

Both boards run slcan_bridge and their CAN connectors are wired together
(CANH-CANH, CANL-CANL, GND, terminated at both ends). The script speaks SLCAN
to each ST-LINK virtual COM port and checks that frames sent by one board
arrive unchanged at the other, plus the firmware's error handling.

Every test starts from the same state: both channels reopened at the selected
bitrates, timestamps off, status flags cleared.

    ./test-boards.py                      # uv installs pyserial on the fly
    .venv/bin/python test-boards.py -v    # or use the West venv (has pyserial)
"""

from __future__ import annotations

import argparse
import queue
import random
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

import serial

BEL = "\a"
RESPONSE_TIMEOUT = 1.0  # seconds to wait for ACK/BEL/reply to a command
RX_TIMEOUT = 1.0  # seconds to wait for a frame on the receiving board
TIMESTAMP_WRAP = 60000  # firmware timestamps count ms modulo 60000

# CAN FD DLC -> payload length; DLC 0-8 are also the Classic CAN lengths
FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
FRAME_PREFIXES = "tTrRdDbB"

# 'F' status bits (see README "Status flags"). Missing ACKs make the
# controller retransmit until it is error passive; no TX callback reports them.
F_TX_FULL = 0x02
F_ERROR_PASSIVE = 0x20

BITRATES = ("10k", "20k", "50k", "100k", "125k", "250k", "500k", "800k", "1M")
DATA_BITRATES = ("500k", "1M", "2M", "3M", "4M", "5M")
# Highest Y index each board accepts, by its 'N' reply. Y5 (5 Mbit/s) needs an
# FDCAN clock that 5 Mbit/s divides: the C092RC's 48 MHz HSE does not (9.6
# time quanta), the H7S3L8's 60 MHz PLL2 does (12).
MAX_DATA_BITRATE = {"NC092": 4, "NH7S3": 5}


class TestFailure(Exception):
    pass


def show(reply: str) -> str:
    """Readable form of a firmware reply."""
    if reply == "":
        return "ACK"
    if reply == BEL:
        return "BEL"
    return repr(reply)


@dataclass(frozen=True)
class Frame:
    id: int
    dlc: int
    data: bytes = b""
    ext: bool = False
    rtr: bool = False
    fd: bool = False
    brs: bool = False
    timestamp: int | None = field(default=None, compare=False)

    @property
    def prefix(self) -> str:
        if self.fd:
            p = "b" if self.brs else "d"
        else:
            p = "r" if self.rtr else "t"
        return p.upper() if self.ext else p

    @property
    def ack(self) -> str:
        """Reply to a successful transmit command: 'z' for 11-bit, 'Z' for 29-bit IDs."""
        return "Z" if self.ext else "z"

    def encode(self) -> str:
        can_id = f"{self.id:08X}" if self.ext else f"{self.id:03X}"
        return f"{self.prefix}{can_id}{self.dlc:X}{self.data.hex().upper()}"

    def __str__(self) -> str:
        ts = "" if self.timestamp is None else f" @{self.timestamp}ms"
        return self.encode() + ts


def data_frame(can_id: int, data: bytes, *, ext=False, fd=False, brs=False) -> Frame:
    return Frame(can_id, FD_LENGTHS.index(len(data)), bytes(data), ext=ext, fd=fd, brs=brs)


def rtr_frame(can_id: int, dlc: int, *, ext=False) -> Frame:
    return Frame(can_id, dlc, ext=ext, rtr=True)


def parse_frame(line: str) -> Frame:
    """Parse a received-frame line (without '\\r'), with optional 4-digit timestamp."""
    prefix = line[0]
    ext = prefix.isupper()
    rtr = prefix in "rR"
    fd = prefix in "dDbB"
    id_digits = 8 if ext else 3
    if not re.fullmatch(r"[0-9A-F]*", line[1:]) or len(line) < 2 + id_digits:
        raise ValueError
    dlc = int(line[1 + id_digits], 16)
    if dlc > (15 if fd else 8):
        raise ValueError
    data_end = 2 + id_digits + (0 if rtr else 2 * FD_LENGTHS[dlc])
    trailer = line[data_end:]
    if len(line) < data_end or len(trailer) not in (0, 4):
        raise ValueError
    return Frame(
        id=int(line[1 : 1 + id_digits], 16),
        dlc=dlc,
        data=bytes.fromhex(line[2 + id_digits : data_end]),
        ext=ext,
        rtr=rtr,
        fd=fd,
        brs=prefix in "bB",
        timestamp=int(trailer, 16) if trailer else None,
    )


class SlcanPort:
    """One board. A reader thread splits the serial stream into command
    replies (ACK, BEL, Fxx, V..., N...) and received CAN frames."""

    def __init__(self, name: str, path: str, baud: int, verbose: bool):
        self.name = name
        self.verbose = verbose
        self.ser = serial.Serial(path, baud, timeout=0.05)
        self.responses: queue.Queue[str] = queue.Queue()
        self.frames: queue.Queue[Frame | str] = queue.Queue()  # str: malformed line
        self.read_error: Exception | None = None
        self.rx_bytes = 0  # total bytes received, for diagnosing a silent board
        self.max_data_bitrate = 0  # highest accepted Y index, set by probe()
        self._line = bytearray()  # bytes since the last '\r'
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name=f"{name}-rx", daemon=True)
        self._reader.start()

    def _log(self, arrow: str, text: str) -> None:
        if self.verbose:
            print(f"    {time.monotonic():.4f} {self.name} {arrow} {text!r}")

    def _read_loop(self) -> None:
        line = self._line
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except (serial.SerialException, OSError) as e:
                self.read_error = e
                return
            self.rx_bytes += len(chunk)
            for byte in chunk:
                if byte == 0x07:  # BEL is always sent on its own, never inside a line
                    self._log("<-", BEL)
                    self.responses.put(BEL)
                elif byte == 0x0D:
                    self._dispatch(line.decode("ascii", "replace"))
                    line.clear()
                else:
                    line.append(byte)

    def _dispatch(self, line: str) -> None:
        self._log("<-", line + "\r")
        if line and line[0] in FRAME_PREFIXES:
            try:
                self.frames.put(parse_frame(line))
            except ValueError:
                self.frames.put(line)
        else:
            self.responses.put(line)

    def write(self, raw: str) -> None:
        self._log("->", raw)
        self.ser.write(raw.encode("ascii"))

    def response(self, timeout: float = RESPONSE_TIMEOUT) -> str:
        try:
            return self.responses.get(timeout=timeout)
        except queue.Empty:
            if self.read_error:
                raise TestFailure(f"{self.name}: serial read failed: {self.read_error}") from None
            msg = f"{self.name}: no reply within {timeout} s"
            if self.rx_bytes == 0:
                msg += " (no byte received since the port was opened)"
            elif self._line:
                msg += f" (unterminated input {bytes(self._line)!r})"
            raise TestFailure(msg) from None

    def command(self, cmd: str) -> str:
        self.write(cmd + "\r")
        return self.response()

    def expect(self, cmd: str, reply: str) -> None:
        got = self.command(cmd)
        if got != reply:
            raise TestFailure(f"{self.name}: {cmd!r} answered {show(got)}, expected {show(reply)}")

    def expect_ack(self, cmd: str) -> None:
        self.expect(cmd, "")

    def expect_nack(self, cmd: str) -> None:
        self.expect(cmd, BEL)

    def send(self, frame: Frame, retry: bool = False) -> int:
        """Transmit a frame. With retry, a BEL (TX mailboxes full) is retried
        until RESPONSE_TIMEOUT; returns the number of retries."""
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        retries = 0
        while True:
            got = self.command(frame.encode())
            if got == frame.ack:
                return retries
            if got != BEL or not retry:
                raise TestFailure(f"{self.name}: {frame} answered {show(got)}, expected {show(frame.ack)}")
            if time.monotonic() > deadline:
                raise TestFailure(f"{self.name}: TX mailboxes stayed full for {RESPONSE_TIMEOUT} s")
            retries += 1
            time.sleep(0.0005)

    def receive(self, timeout: float = RX_TIMEOUT) -> Frame:
        try:
            item = self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TestFailure(f"{self.name}: no frame received within {timeout} s") from None
        if isinstance(item, str):
            raise TestFailure(f"{self.name}: malformed frame line {item!r}")
        return item

    def status(self) -> int:
        """Read and clear the 'F' status flags."""
        got = self.command("F")
        m = re.fullmatch(r"F([0-9A-F]{2})", got)
        if not m:
            raise TestFailure(f"{self.name}: 'F' answered {show(got)}")
        return int(m[1], 16)

    def drain(self) -> tuple[list[str], list[Frame | str]]:
        replies, frames = [], []
        while not self.responses.empty():
            replies.append(self.responses.get_nowait())
        while not self.frames.empty():
            frames.append(self.frames.get_nowait())
        return replies, frames

    def expect_silence(self, duration: float = 0.1) -> None:
        """Nothing may arrive: no stray replies, no frames (e.g. own echo)."""
        time.sleep(duration)
        replies, frames = self.drain()
        if replies:
            raise TestFailure(f"{self.name}: unexpected replies {[show(r) for r in replies]}")
        if frames:
            raise TestFailure(f"{self.name}: unexpected frames {[str(f) for f in frames]}")

    def close(self) -> None:
        self._stop.set()
        self._reader.join()
        self.ser.close()

    def probe(self) -> str | None:
        """Check that the board answers 'V' and 'N'; returns an error message or None."""
        self.write("\r")  # end any partial line a previous run left behind
        time.sleep(0.2)
        self.drain()
        try:
            v = self.command("V")
            n = self.command("N")
        except TestFailure as e:
            return str(e)
        if not re.fullmatch(r"V[0-9A-F]{4}", v):
            return f"{self.name}: 'V' answered {show(v)}"
        if n not in MAX_DATA_BITRATE:
            return f"{self.name}: 'N' answered {show(n)}, not a known board ({' '.join(MAX_DATA_BITRATE)})"
        self.max_data_bitrate = MAX_DATA_BITRATE[n]
        return None


class Bench:
    def __init__(self, a: SlcanPort, b: SlcanPort, args: argparse.Namespace):
        self.a = a
        self.b = b
        self.args = args
        self.rng = random.Random(args.seed)

    @property
    def ports(self) -> tuple[SlcanPort, SlcanPort]:
        return self.a, self.b

    def directions(self) -> tuple[tuple[SlcanPort, SlcanPort], ...]:
        return (self.a, self.b), (self.b, self.a)

    def reset(self) -> None:
        """Close both channels, discard leftovers, reopen with the test settings."""
        for p in self.ports:
            p.write("\rC\r")  # '\r' ends any partial line a previous run left behind
        time.sleep(0.2)
        for p in self.ports:
            p.drain()
        for p in self.ports:
            p.expect_ack(f"S{self.args.bitrate}")
            p.expect_ack(f"Y{self.args.data_bitrate}")
            p.expect_ack("Z0")
            p.expect_ack("O")
            p.status()  # clear latched flags


def transfer(tx: SlcanPort, rx: SlcanPort, frame: Frame) -> Frame:
    tx.send(frame)
    got = rx.receive()
    if got != frame:
        raise TestFailure(f"{tx.name}->{rx.name}: sent {frame}, received {got}")
    return got


def expect_flags(port: SlcanPort, allowed: int = 0, required: int = 0) -> int:
    flags = port.status()
    if flags & ~allowed & ~required or flags & required != required:
        raise TestFailure(
            f"{port.name}: status F{flags:02X}, allowed {allowed | required:02X}, "
            f"required {required:02X}"
        )
    return flags


# ---------------------------------------------------------------- tests

TESTS: list[Callable[[Bench], str | None]] = []


def test(fn: Callable[[Bench], str | None]) -> Callable[[Bench], str | None]:
    TESTS.append(fn)
    return fn


@test
def identification(bench: Bench) -> str:
    """'V' and 'N' return version and serial number."""
    out = []
    for p in bench.ports:
        v, n = p.command("V"), p.command("N")
        if not re.fullmatch(r"V[0-9A-F]{4}", v) or not re.fullmatch(r"N\w{4}", n):
            raise TestFailure(f"{p.name}: V -> {show(v)}, N -> {show(n)}")
        out.append(f"{p.name} {v} {n}")
    return ", ".join(out)


@test
def pipelined_commands(bench: Bench) -> None:
    """64 commands in one write each get a reply: no input byte is lost at full baud rate."""
    for p in bench.ports:
        p.write("V\r" * 64)
        for i in range(64):
            if not re.fullmatch(r"V[0-9A-F]{4}", got := p.response()):
                raise TestFailure(f"{p.name}: reply {i + 1} of 64 to back-to-back 'V' was {show(got)}")


@test
def classic_standard(bench: Bench) -> None:
    """11-bit data frames with 0-8 bytes, both directions."""
    for tx, rx in bench.directions():
        for can_id in (0x000, 0x7FF, bench.rng.randrange(0x800)):
            for n in range(9):
                transfer(tx, rx, data_frame(can_id, bench.rng.randbytes(n)))


@test
def classic_extended(bench: Bench) -> None:
    """29-bit data frames with 0-8 bytes, both directions."""
    for tx, rx in bench.directions():
        for can_id in (0x00000000, 0x1FFFFFFF, bench.rng.randrange(1 << 29)):
            for n in range(9):
                transfer(tx, rx, data_frame(can_id, bench.rng.randbytes(n), ext=True))


@test
def remote_frames(bench: Bench) -> None:
    """11- and 29-bit RTR frames with DLC 0-8, both directions."""
    for tx, rx in bench.directions():
        for dlc in range(9):
            transfer(tx, rx, rtr_frame(bench.rng.randrange(0x800), dlc))
            transfer(tx, rx, rtr_frame(bench.rng.randrange(1 << 29), dlc, ext=True))


@test
def fd_frames(bench: Bench) -> None:
    """CAN FD frames, all 16 DLCs, 11/29-bit IDs, without and with BRS."""
    for tx, rx in bench.directions():
        for brs in (False, True):
            for ext in (False, True):
                for n in FD_LENGTHS:
                    can_id = bench.rng.randrange(1 << 29 if ext else 0x800)
                    data = bench.rng.randbytes(n)
                    transfer(tx, rx, data_frame(can_id, data, ext=ext, fd=True, brs=brs))


@test
def lowercase_hex(bench: Bench) -> None:
    """Lowercase hex digits in commands are accepted."""
    a, b = bench.ports
    a.expect("t7ff2abcd", "z")
    expected = data_frame(0x7FF, b"\xab\xcd")
    got = b.receive()
    if got != expected:
        raise TestFailure(f"sent t7ff2abcd, received {got}, expected {expected}")


@test
def line_terminators(bench: Bench) -> None:
    """'\\r', '\\n' and '\\r\\n' each end exactly one command."""
    a, b = bench.ports
    for terminator in ("\r", "\n", "\r\n"):
        frame = data_frame(0x123, bench.rng.randbytes(2))
        a.write(frame.encode() + terminator)
        got = a.response()
        if got != frame.ack:
            raise TestFailure(f"{frame} + {terminator!r} answered {show(got)}")
        if (rx := b.receive()) != frame:
            raise TestFailure(f"sent {frame}, received {rx}")
        a.expect_silence(0.05)  # a second ACK would mean CRLF counted twice
    a.expect_ack("")  # empty command


@test
def rejected_commands(bench: Bench) -> None:
    """Malformed and out-of-state commands get BEL and send nothing."""
    a, b = bench.ports
    bad = [
        "t12",  # too short
        "t1231",  # DLC 1 without data
        "t123000",  # DLC 0 with trailing data
        "t12310000",  # DLC 1 with 2 data bytes
        "t8000",  # ID wider than 11 bits
        "T200000000",  # ID wider than 29 bits
        "t1239" + "00" * 12,  # DLC 9 in a Classic CAN frame
        "r1231AA",  # RTR with data
        "t12G0",  # non-hex ID
        "t123X",  # non-hex DLC
        "t1231ZZ",  # non-hex data
        "d123G",  # non-hex FD DLC
        "S6",  # bitrate change while open
        "Y2",  # data bitrate change while open
        "Z1",  # timestamp change while open
        "O",  # already open
        "L",  # already open
        "C1",  # trailing characters
        "F1",  # trailing characters
        "X",  # unknown command
    ]
    for cmd in bad:
        a.expect_nack(cmd)
    # An over-long line gets a single BEL and is discarded entirely
    a.write("t" + "0" * 299 + "\r")
    if (got := a.response()) != BEL:
        raise TestFailure(f"300-character line answered {show(got)}, expected BEL")
    a.expect_silence(0.1)
    b.expect_silence(0.0)
    expect_flags(a)
    transfer(a, b, data_frame(0x123, b"\x01"))  # parser still in sync


@test
def closed_channel(bench: Bench) -> None:
    """Closed channel: transmit and 'F' are refused, S0-S8 and the board's Y range accepted, higher Y refused."""
    a, b = bench.ports
    a.expect_ack("C")
    y_max = a.max_data_bitrate
    refused = ["C", "F", "t1230", "d1230", "S9", "Y6", "Z2", "S", "Y", "Z"]
    refused += [f"Y{i}" for i in range(y_max + 1, len(DATA_BITRATES))]
    for cmd in refused:
        a.expect_nack(cmd)
    rejected = []
    for cmd in [f"S{i}" for i in range(9)] + [f"Y{i}" for i in range(y_max + 1)]:
        if a.command(cmd) != "":
            rejected.append(cmd)
    a.expect_ack(f"S{bench.args.bitrate}")
    a.expect_ack(f"Y{bench.args.data_bitrate}")
    if rejected:
        raise TestFailure(f"bitrate commands rejected: {' '.join(rejected)}")
    a.expect_ack("L")
    a.expect_ack("C")
    a.expect_ack("O")
    transfer(a, b, data_frame(0x123, b"\x01"))
    transfer(b, a, data_frame(0x321, b"\x02"))


@test
def timestamps(bench: Bench) -> str:
    """Z1 appends a millisecond timestamp to received frames; Z0 does not."""
    a, b = bench.ports
    for p in bench.ports:
        p.expect_ack("C")
    b.expect_ack("Z1")
    for p in bench.ports:
        p.expect_ack("O")

    first = transfer(a, b, data_frame(0x100, b"\x01"))
    host_start = time.monotonic()
    time.sleep(0.2)
    host_delta = (time.monotonic() - host_start) * 1000
    second = transfer(a, b, data_frame(0x100, b"\x02"))
    for f in (first, second):
        if f.timestamp is None or f.timestamp >= TIMESTAMP_WRAP:
            raise TestFailure(f"{b.name}: bad timestamp in {f}")
    board_delta = (second.timestamp - first.timestamp) % TIMESTAMP_WRAP
    if abs(board_delta - host_delta) > 25:
        raise TestFailure(f"timestamp delta {board_delta} ms, host measured {host_delta:.0f} ms")

    untimed = transfer(b, a, data_frame(0x200, b"\x03"))
    if untimed.timestamp is not None:
        raise TestFailure(f"{a.name}: timestamp {untimed.timestamp} with Z0")
    return f"board {board_delta} ms vs host {host_delta:.0f} ms"


def seq_classic(seq: int) -> Frame:
    return data_frame(0x0F0, seq.to_bytes(4, "big") * 2)


def seq_fd(seq: int) -> Frame:
    return data_frame(0x0F1, seq.to_bytes(4, "big") * 16, fd=True, brs=True)


def stream_seq(frame: Frame | str, make: Callable[[int], Frame]) -> int | None:
    """Sequence number of a burst frame, None for anything else."""
    if not isinstance(frame, Frame) or len(frame.data) < 4:
        return None
    seq = int.from_bytes(frame.data[:4], "big")
    return seq if make(seq) == frame else None


def received_seqs(rx: SlcanPort, make: Callable[[int], Frame], first: Frame) -> str:
    """Sequence numbers of `first` and every frame still arriving after it, as
    ranges ('?' for a foreign frame). After a mismatch at frame k, "k-1 - n-1"
    is one duplicate in an otherwise complete stream; "k-1 - n-2" is a stream
    running one frame behind that never delivers its last frame."""
    frames: list[Frame | str] = [first]
    while True:
        try:
            frames.append(rx.frames.get(timeout=RX_TIMEOUT))
        except queue.Empty:
            break
    seqs = [stream_seq(f, make) for f in frames]
    parts: list[str] = []
    i = 0
    while i < len(seqs):
        start = seqs[i]
        if start is None:
            parts.append("?")
            i += 1
            continue
        j = i
        while j + 1 < len(seqs) and seqs[j + 1] == seqs[j] + 1:
            j += 1
        parts.append(str(start) if i == j else f"{start}-{seqs[j]}")
        i = j + 1
    return " ".join(parts)


def run_burst(
    bench: Bench, streams: list[tuple[SlcanPort, SlcanPort, Callable[[int], Frame]]]
) -> str:
    """Each tx sends burst frames with one ID (lockstep, BEL retried) in its own
    thread; each rx must get all of them, in order, without drops."""
    n = bench.args.burst
    retries: dict[str, int] = {}
    errors: list[BaseException] = []

    def pump(tx: SlcanPort, make: Callable[[int], Frame]) -> None:
        try:
            retries[tx.name] = sum(tx.send(make(seq), retry=True) for seq in range(n))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=pump, args=(tx, make)) for tx, _, make in streams]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    if errors:
        raise errors[0]

    for tx, rx, make in streams:
        for seq in range(n):
            got, expected = rx.receive(), make(seq)
            if got != expected:
                rest = received_seqs(rx, make, got)
                flags = ", ".join(f"{p.name} F{p.status():02X}" for p in bench.ports)
                raise TestFailure(
                    f"{tx.name}->{rx.name} frame {seq}: received {got}, expected {expected}; "
                    f"from there on received {rest}, expected {seq}-{n - 1}; {flags}"
                )
    for p in bench.ports:
        expect_flags(p, allowed=F_TX_FULL)  # BEL retries are fine, drops/errors are not

    rate = n * len(streams) / elapsed
    paths = ", ".join(f"{tx.name}->{rx.name} {retries[tx.name]} BEL" for tx, rx, _ in streams)
    return f"{n} frames per stream in {elapsed:.2f} s ({rate:.0f} frames/s; {paths})"


@test
def burst_classic(bench: Bench) -> str:
    """Burst of 8-byte Classic frames A->B, then B->A."""
    a, b = bench.ports
    return "; ".join(
        (run_burst(bench, [(a, b, seq_classic)]), run_burst(bench, [(b, a, seq_classic)]))
    )


@test
def burst_fd(bench: Bench) -> str:
    """Burst of 64-byte CAN FD BRS frames A->B, then B->A."""
    a, b = bench.ports
    return "; ".join((run_burst(bench, [(a, b, seq_fd)]), run_burst(bench, [(b, a, seq_fd)])))


@test
def burst_bidirectional(bench: Bench) -> str:
    """Both boards transmit at once (arbitration): A Classic, B CAN FD."""
    a, b = bench.ports
    return run_burst(bench, [(a, b, seq_classic), (b, a, seq_fd)])


@test
def listen_only(bench: Bench) -> None:
    """A board opened with 'L' neither ACKs nor transmits but receives; 'C' discards pending frames."""
    a, b = bench.ports
    b.expect_ack("C")
    b.expect_ack("L")
    b.send(data_frame(0x555, b"\x55"))  # queued, but must never reach the bus
    sent = data_frame(0x123, b"\x01")
    a.send(sent)  # nobody ACKs it, so A retransmits it until 'C'
    time.sleep(0.1)
    a.expect_silence(0.0)  # B's frame never reached the bus
    expect_flags(a, allowed=0xFF, required=F_ERROR_PASSIVE)

    # Receivers ignore the ACK slot, so every retransmission is a valid frame for B
    for p in bench.ports:
        p.expect_ack("C")
    time.sleep(0.1)  # let B's serial TX buffer drain
    replies, frames = b.drain()
    if replies:
        raise TestFailure(f"B: unexpected replies {[show(r) for r in replies]}")
    if not frames or any(f != sent for f in frames):
        others = sorted({str(f) for f in frames if f != sent})
        raise TestFailure(f"B: expected only copies of {sent}, got {len(frames)} frames, others {others}")

    # Reopen both: the aborted frames must not show up afterwards
    for p in bench.ports:
        p.expect_ack("O")
    transfer(a, b, data_frame(0x124, b"\x02"))
    transfer(b, a, data_frame(0x556, b"\x56"))


@test
def no_ack(bench: Bench) -> str:
    """Without a receiver, frames fill the TX mailboxes: BEL, F bit 1, error passive."""
    a, b = bench.ports
    b.expect_ack("C")
    accepted = 0
    for i in range(10):
        frame = data_frame(0x123, bytes([i]))
        got = a.command(frame.encode())
        if got == BEL:
            break
        if got != frame.ack:
            raise TestFailure(f"{a.name}: transmit answered {show(got)}")
        accepted += 1
    else:
        raise TestFailure(f"{a.name}: 10 unacknowledged frames accepted, expected BEL")
    if accepted == 0:
        raise TestFailure(f"{a.name}: first frame already refused")
    flags = expect_flags(a, allowed=0xFF, required=F_TX_FULL | F_ERROR_PASSIVE)

    a.expect_ack("C")
    for p in bench.ports:
        p.expect_ack("O")
    transfer(a, b, data_frame(0x124, b"\xaa"))  # aborted frames are not sent
    transfer(b, a, data_frame(0x125, b"\xbb"))
    return f"{accepted} frames queued before BEL, F{flags:02X}"


@test
def tx_order(bench: Bench) -> str:
    """Frames pending in the TX mailboxes leave in the order sent, not by ID priority."""
    a, b = bench.ports
    b.expect_ack("C")  # no ACK: every frame A accepts stays pending
    sent: list[Frame] = []
    for can_id in range(0x700, 0x000, -0x100):  # descending: lowest ID queued last
        frame = data_frame(can_id, bytes([len(sent)]))
        got = a.command(frame.encode())
        if got == BEL:
            break
        if got != frame.ack:
            raise TestFailure(f"{a.name}: transmit answered {show(got)}")
        sent.append(frame)
    else:
        raise TestFailure(f"{a.name}: {len(sent)} unacknowledged frames accepted, expected BEL")
    if len(sent) < 2:
        raise TestFailure(f"{a.name}: only {len(sent)} frame(s) queued, order cannot be checked")

    b.expect_ack("O")  # B now ACKs: A's pending frames go out one by one
    got = [b.receive() for _ in sent]
    if got != sent:
        raise TestFailure(
            f"{a.name}->{b.name}: sent {[str(f) for f in sent]}, received {[str(f) for f in got]}"
        )
    b.expect_silence()
    return f"{len(sent)} pending frames, IDs {' '.join(f'{f.id:03X}' for f in sent)}"


# ---------------------------------------------------------------- runner


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port-a", default="/dev/ttyACM0", help="board A (default: %(default)s)")
    parser.add_argument("--port-b", default="/dev/ttyACM1", help="board B (default: %(default)s)")
    parser.add_argument("--baud", type=int, default=1000000, help="serial baud (default: %(default)s)")
    parser.add_argument(
        "--bitrate", type=int, choices=range(9), default=6, metavar="0-8",
        help="nominal bitrate index S0-S8: " + " ".join(BITRATES) + " (default: 6)",
    )
    parser.add_argument(
        "--data-bitrate", type=int, choices=range(len(DATA_BITRATES)), default=2, metavar="0-5",
        help="CAN FD data bitrate index Y0-Y5: " + " ".join(DATA_BITRATES)
        + " (default: 2; Y5 needs two boards that support it)",
    )
    parser.add_argument("--burst", type=int, default=1000, help="frames per burst stream (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=random.randrange(1 << 32), help="random seed for IDs/data")
    parser.add_argument("-k", dest="select", action="append", default=[], help="run tests whose name contains this (repeatable)")
    parser.add_argument("--list", action="store_true", help="list tests and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="log all serial traffic")
    args = parser.parse_args()

    tests = [t for t in TESTS if not args.select or any(s in t.__name__ for s in args.select)]
    if args.list or not tests:
        for t in TESTS:
            print(f"{t.__name__:20} {t.__doc__}")
        return 0 if args.list else 2

    try:
        a = SlcanPort("A", args.port_a, args.baud, args.verbose)
    except serial.SerialException as e:
        print(f"cannot open board A: {e}", file=sys.stderr)
        return 2
    try:
        b = SlcanPort("B", args.port_b, args.baud, args.verbose)
    except serial.SerialException as e:
        a.close()
        print(f"cannot open board B: {e}", file=sys.stderr)
        return 2

    print(f"A={args.port_a} B={args.port_b} S{args.bitrate} ({BITRATES[args.bitrate]}) "
          f"Y{args.data_bitrate} ({DATA_BITRATES[args.data_bitrate]}) seed={args.seed}")
    bench = Bench(a, b, args)
    errors = [e for e in (a.probe(), b.probe()) if e]
    if errors:
        for e in errors:
            print(f"board check failed: {e}", file=sys.stderr)
        print("Check that the board runs slcan_bridge, --baud matches the firmware "
              "(1000000), and no other program has the port open.", file=sys.stderr)
        a.close()
        b.close()
        return 2
    too_slow = [p.name for p in bench.ports if args.data_bitrate > p.max_data_bitrate]
    if too_slow:
        print(f"--data-bitrate {args.data_bitrate}: Y{args.data_bitrate} is not supported by board "
              f"{' '.join(too_slow)}", file=sys.stderr)
        a.close()
        b.close()
        return 2
    failed = []
    try:
        for t in tests:
            name = t.__name__
            try:
                bench.reset()
                info = t(bench)
                for p in bench.ports:
                    p.expect_silence(0.05)
            except TestFailure as e:
                failed.append(name)
                print(f"FAIL {name}: {e}")
            except Exception:
                failed.append(name)
                print(f"FAIL {name}:")
                traceback.print_exc()
            else:
                print(f"PASS {name}" + (f": {info}" if info else ""))
    except KeyboardInterrupt:
        print("interrupted")
        failed.append("<interrupted>")
    finally:
        for p in bench.ports:
            try:
                p.write("\rC\r")
                time.sleep(0.05)
            except serial.SerialException:
                pass
            p.close()

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed" + (f", failed: {' '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
