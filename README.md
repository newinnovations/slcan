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
| 7   | Bus error: a transmit failed (e.g. missing ACK) or the controller went bus-off      |

### Transmit acknowledgement

`\r` after a transmit command means the frame was queued in a CAN TX mailbox, not that it was sent. Transmit failures show up in `F` bit 7. If no node ACKs the frames, the mailboxes stay full and later transmit commands are answered with BEL and set bit 1.

### Serial throughput

At 1 Mbaud the serial link carries about 100 kB/s. A busy bus produces more than that, for example ~198 kB/s for classic 8-byte frames at 1 Mbit/s, or ~460 kB/s for 64-byte CAN FD frames at a 2 Mbit/s data rate. The firmware then drops whole received frames (never partial lines) and sets `F` bit 0.

## Review notes

### Fixed

1. The UART TX interrupt discarded bytes that `uart_fifo_fill()` did not accept. The STM32 driver accepts only 1-2 bytes per call with the FIFO disabled, so most output was lost. It now uses `ring_buf_get_claim`/`ring_buf_get_finish`, so unsent bytes stay queued.
2. `can_send()` with a NULL callback waits `K_FOREVER` for transmit completion. Without an ACKing node, the main loop hung forever. Transmission is now asynchronous with `K_NO_WAIT`.
3. A full serial TX buffer truncated SLCAN lines mid-frame. Received frames are now dropped whole. Command responses wait for buffer space.
4. `CONFIG_CAN_ACCEPT_RTR` was unset, so the driver rejected every incoming RTR frame. It is now enabled in `prj.conf`.
5. `F` always returned `F00`. It now reports real flags (see above).

### Open concerns

- `Z` (timestamps) is ACKed, but no timestamps are added. A host that enables timestamps will misparse every received frame.
- The main loop polls every 100 µs (`k_sleep(K_USEC(100))`). Waiting with `k_poll` on the CAN message queue and a semaphore given by the UART ISR would avoid the constant wakeups.
- `Y` enables CAN FD mode permanently; nothing disables it again. Without `Y`, `d`/`D`/`b`/`B` are rejected (`-ENOTSUP`).
- The results of the startup `can_set_bitrate()`/`can_set_bitrate_data()` calls are ignored. `S` updates `current_bitrate` before the bitrate change succeeds.
- After a command line overflows the 256-byte buffer, the rest of the line is parsed as a new command instead of being discarded up to the next `\r`.
- Frame commands accept trailing junk (`len < expected_len` instead of `!=`). IDs beyond 11/29 bits are rejected only by `can_send()` (`-EINVAL`).
- A host that sends `\r\n` gets two ACKs, because the `\n` counts as an empty command.
- `CONFIG_MAIN_STACK_SIZE=1024` is tight with `cmd_buf[256]`, a CAN FD `struct can_frame` and the 144-byte formatter buffer on the stack. This was not measured.
- The `&fdcan1 { status = "okay"; }` overlay entry is redundant; the board DTS already enables `fdcan1` and its MCP2562FD transceiver (max 5 Mbit/s).
- Whether the ST-LINK virtual COM port handles 1 Mbaud has not been checked on hardware.
