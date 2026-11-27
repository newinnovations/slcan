# SLCAN Bridge

This repository contains the Zephyr-based SLCAN bridge project for the Nucleo C092RC board.

## Overview

The SLCAN bridge project enables communication between a host system and CAN networks using the Nucleo C092RC board. It implements the SLCAN protocol over UART and supports both Classic CAN and CAN FD frames. The bridge is built on the Zephyr RTOS, leveraging its device drivers and message queue mechanisms for efficient CAN frame handling.

## Zephyr project setup

### Requirements

Run this setup on Linux with a supported host architecture: `x86_64` or `aarch64`. The setup script requires Bash, `uv`, Git, CMake (3.20 or newer), and Ninja, plus internet access. Device tree compiler and other host tools come from the Zephyr SDK.

### Install

From the repository root, run:

```sh
./zephyr-init.sh
```

The script:

1. Creates a Python virtual environment (with pip, which `west packages pip` needs) and installs West into it.
2. Configures this directory as the West workspace root and shallowly updates the projects pinned by `west.yml`.
3. Installs the Python requirements of those projects.
4. Runs `west sdk install`, which downloads the Zephyr SDK version named in `deps/zephyr/SDK_VERSION`, checks it against the release `sha256.sum`, extracts it into this directory, and runs its `setup.sh` to install the `arm-zephyr-eabi` GNU toolchain and the host tools.

The script is safe to rerun. If an SDK of the same version is already installed, West skips the download but runs `setup.sh` again. West looks for an installed SDK wherever CMake finds one (the user package registry, `~`, `/opt`, ...). If it picks one outside this directory, the script stops with an error; remove or unregister that SDK and rerun.

Files created in this repository directory:

```text
.venv/                    Python environment with West
.west/config              West workspace configuration
deps/zephyr/              Zephyr
deps/modules/             Modules imported from Zephyr's manifest
zephyr-sdk-<version>/     Zephyr SDK
build/                    Build output of make.sh
```

Outside it, the SDK's `setup.sh` registers the SDK in the CMake user package registry under `~/.cmake/packages/Zephyr-sdk/`.

### Build

To build the configured project after installation, run:

```sh
./make.sh
```

It sets `ZEPHYR_SDK_INSTALL_DIR` so the build prefers the SDK in this directory over any other registered one.

### Clean

To remove the generated local environment, workspace, build output, SDK, and the SDK's CMake package registry entry, run:

```sh
./clean-all.sh
```

### Test with two boards

Flash both boards and wire their CAN connectors together (CANH-CANH, CANL-CANL, GND, terminated at both ends). Then run:

```sh
./test-boards.py                     # board A /dev/ttyACM0, board B /dev/ttyACM1
./test-boards.py --list              # show the tests
./test-boards.py -k burst -v         # run tests matching "burst", log all serial traffic
```

`uv` provides `pyserial`; `.venv/bin/python test-boards.py` works as well. Options set the ports (`--port-a`, `--port-b`), bitrates (`--bitrate 0-8`, `--data-bitrate 0-4`), burst length (`--burst`) and random seed (`--seed`). Before each test both channels are reopened at the chosen bitrates. The tests send Classic, extended, RTR and CAN FD frames (all DLCs, with and without BRS) both ways and compare them. They also check rejected commands, line endings, timestamps, sequenced bursts in each direction and both at once, listen-only mode, the TX mailboxes filling when no node ACKs, and that pending frames with descending IDs leave in the order sent. The exit code is 0 only if every test passes.

Before the tests, the script sends `V` to each board. If a board does not answer, it reports whether any bytes arrived at all (none: board not running the firmware, wrong port, or the port held by another program; unterminated bytes: baud mismatch) and exits with code 2.

![Test with two Nucleo C092RC boards connected via CAN](doc/two-boards.jpg "Test with two Nucleo C092RC boards connected via CAN")

```text
A=/dev/ttyACM0 B=/dev/ttyACM1 S6 (500k) Y2 (2M) seed=3796532915
PASS identification: A V0102 NC092, B V0102 NC092
PASS pipelined_commands
PASS classic_standard
PASS classic_extended
PASS remote_frames
PASS fd_frames
PASS lowercase_hex
PASS line_terminators
PASS rejected_commands
PASS closed_channel
PASS timestamps: board 204 ms vs host 201 ms
PASS burst_classic: 1000 frames per stream in 1.99 s (502 frames/s; A->B 0 BEL); 1000 frames per stream in 2.00 s (501 frames/s; B->A 0 BEL)
PASS burst_fd: 1000 frames per stream in 3.10 s (322 frames/s; A->B 0 BEL); 1000 frames per stream in 3.24 s (309 frames/s; B->A 0 BEL)
PASS burst_bidirectional: 1000 frames per stream in 3.07 s (651 frames/s; A->B 0 BEL, B->A 0 BEL)
PASS listen_only
PASS no_ack: 3 frames queued before BEL, F22
PASS tx_order: 3 pending frames, IDs 700 600 500

17/17 passed
```

## Firmware behavior

### Status flags (`F`)

`F` works only while the channel is open, as in LAWICEL. It returns `Fxx\r`, where `xx` is a hex bitmask. Latched flags are cleared by each `F` read and by `O`/`L`. The current controller state (error warning, error passive, bus-off) is added to every reply.

| Bit | Meaning                                                                             |
| --- | ----------------------------------------------------------------------------------- |
| 0   | Received CAN frame dropped: the RX message queue or the serial TX buffer was full   |
| 1   | Frame not queued: no free CAN TX mailbox (`t`/`T`/... answered with BEL)            |
| 2   | Error warning                                                                       |
| 3   | Serial input from the host dropped: the UART RX buffer was full                     |
| 5   | Error passive                                                                       |
| 6   | Arbitration lost (reported only in one-shot mode, which this firmware does not use) |
| 7   | Bus-off: the controller went bus-off and discarded the frames in its TX mailboxes   |

### Transmit acknowledgement

`\r` after a transmit command means the frame was queued in a CAN TX mailbox, not that it was sent. A frame that no node ACKs is not reported as failed: the controller retransmits it until a node ACKs it or the channel is closed with `C`, and its transmit error counter rises until it is error passive (`F` bits 2 and 5). Meanwhile the mailboxes stay full, so later transmit commands are answered with BEL and set bit 1.

### CAN FD

`O` and `L` always open the controller in CAN FD mode. Classic CAN frames are sent and received unchanged, and `d`/`D`/`b`/`B` work without a prior `Y`. The data phase defaults to 2 Mbit/s (nominal: 500 kbit/s); `Y0`-`Y4` change it while the channel is closed. `Y5` (5 Mbit/s) is answered with BEL: the FDCAN clock is the 48 MHz HSE, and 48 MHz / 5 Mbit/s = 9.6 time quanta per bit is not an integer. The on-board MCP2562FD transceiver is rated up to 5 Mbit/s.

### Listen-only (`L`)

A channel opened with `L` sends no ACKs and no frames, but it receives. Receivers ignore the ACK slot, so when no other node ACKs a frame, every retransmission of it is a valid frame to the listener: it reports the same frame repeatedly until the sender closes its channel.

### Timestamps (`Z`)

`Z1` turns timestamps on and `Z0` turns them off; both work only while the channel is closed. With timestamps on, every received frame carries four extra hex digits before the `\r`: the arrival time in milliseconds, wrapping at 60000 (`0000`-`EA5F`).

### Command parsing

Commands end with `\r`; a bare `\n` also ends a command, and the `\n` of a `\r\n` pair is ignored. A frame command must have exactly the length its DLC implies (no data for `r`/`R`), and its ID must fit 11 or 29 bits. A line longer than 256 characters gets one BEL, and the rest of it is discarded.

### Serial throughput

At 1 Mbaud the serial link carries about 100 kB/s. A busy bus produces more than that, for example ~198 kB/s for classic 8-byte frames at 1 Mbit/s, or ~460 kB/s for 64-byte CAN FD frames at a 2 Mbit/s data rate. The firmware then drops whole received frames (never partial lines) and sets `F` bit 0.

## Review notes

### Fixed

1. The UART TX interrupt discarded bytes that `uart_fifo_fill()` did not accept. The STM32 driver accepts only 1-2 bytes per call with the FIFO disabled, so most output was lost. It now uses `ring_buf_get_claim`/`ring_buf_get_finish`, so unsent bytes stay queued.
2. `can_send()` with a NULL callback waits `K_FOREVER` for transmit completion. Without an ACKing node, the main loop hung forever. Transmission is now asynchronous with `K_NO_WAIT`.
3. A full serial TX buffer truncated SLCAN lines mid-frame. Received frames are now dropped whole. Command responses wait for buffer space.
4. `CONFIG_CAN_ACCEPT_RTR` was unset, so the driver rejected every incoming RTR frame. It is now enabled in `prj.conf`.
5. `F` always returned `F00`. It now reports real flags (see above).
6. `Z` was ACKed without adding timestamps. `Z1`/`Z0` now turn millisecond timestamps on and off (see above).
7. The main loop woke every 100 µs. It now sleeps in `k_poll` on the CAN RX message queue and a semaphore given when UART data arrives.
8. FD mode was enabled only after `Y` and could not be disabled again. The channel now always opens FD-capable, so `d`/`D`/`b`/`B` work without `Y`.
9. Results of the startup bitrate calls were ignored; the firmware now stops if they fail. `S`/`Y` no longer change state when the bitrate is rejected.
10. After a command line overflowed the 256-byte buffer, the rest of the line was parsed as a new command. It is now discarded up to the end of the line.
11. Frame commands accepted trailing characters and out-of-range IDs. The length must now match the DLC exactly, and IDs must fit 11/29 bits.
12. `\r\n` from the host produced two ACKs. The `\n` of a CRLF pair is now ignored.
13. The overlay's redundant `&fdcan1 { status = "okay"; }` was removed; the board DTS already enables `fdcan1`.
14. The main stack was ~98% full on the transmit path with no overflow detection. The large buffers are now `static` and the MPU stack guard is enabled (see below).
15. At 1 Mbaud, commands sent back to back lost bytes (bytes 3-5 of a burst on hardware), so the board stopped answering after the first multi-command write. USART2 has no RX FIFO, and the interrupt-driven RX path was held off for longer than one byte time (10 µs). The UART now uses the async API with DMA: RX into a circular 256-byte buffer, TX straight from the ring buffer. The `pipelined_commands` test sends 64 commands in one write.
16. The FDCAN TX mailboxes ran as a priority queue: pending frames left lowest ID first, and frames with the same ID in unpredictable order, which breaks protocols such as ISO-TP that rely on frame order. `CONFIG_CAN_MCAN_TX_FIFO` now sends them in the order the host queued them. The `tx_order` test queues descending IDs while no node ACKs and checks the receive order.

### Stack usage

Per-function frame sizes come from a `-fstack-usage` build. Calls through driver function pointers (e.g. `api->send`) were followed by hand.

| Worst path (transmit command)                                        | Before      | After      |
| -------------------------------------------------------------------- | ----------- | ---------- |
| `z_thread_entry` + `bg_thread_main`                                  | 72 B        | 72 B       |
| `main` (command parser and formatter inlined)                        | 600 B       | 208 B      |
| `handle_tx_frame` → `can_send` → `can_mcan_send` → message RAM write | ~304 B      | ~304 B     |
| Exception frame when an interrupt arrives                            | 32 B        | 32 B       |
| **Total of `CONFIG_MAIN_STACK_SIZE=1024`**                           | **~1008 B** | **~616 B** |

`cmd_buf[256]` and the 144-byte formatter buffer are now `static`; only the main thread uses them. `CONFIG_ARM_MPU` and `CONFIG_HW_STACK_PROTECTION` put an MPU guard below each thread stack, so an overflow raises a fault instead of corrupting RAM. Together, the two changes raise RAM use from 13.4 KB to 15.0 KB (of 30 KB) and flash from 25.1 KB to 26.0 KB.
