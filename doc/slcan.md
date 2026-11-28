# SLCAN Bridge Serial ASCII Protocol

This reference describes the ASCII command stream between the host and the SLCAN bridge firmware (`slcan_bridge/src/main.c`) on the Nucleo C092RC and the Nucleo H7S3L8. It covers the serial link, configuration commands, CAN and CAN FD frame encoding, responses, status flags, receive timestamps, and the places where the firmware differs from the original LAWICEL SLCAN protocol that existing host software expects. Both boards run the same firmware and speak the same protocol; the differences are the `Y5` data bit rate and the `N` reply.

## Serial link

| Setting      | Value                                                                                      |
| ------------ | ------------------------------------------------------------------------------------------ |
| Port         | ST-LINK virtual COM port, e.g. `/dev/ttyACM0`: USART2 on the C092RC, USART3 on the H7S3L8  |
| Speed        | 1,000,000 baud, fixed in `boards/nucleo_c092rc.overlay` and `boards/nucleo_h7s3l8.overlay` |
| Framing      | 8 data bits, no parity, 1 stop bit                                                         |
| Flow control | None                                                                                       |

The firmware sends nothing but SLCAN output on this port: boot banner, logging, and console are disabled.

## Wire format and command handling

- A command is one case-sensitive letter followed by its arguments, ending with a carriage return (`CR`, `0x0D`, written `\r` below). Do not insert spaces.
- Hex digits in arguments may be upper or lower case. The firmware always outputs upper-case hex.
- Every command line gets exactly one response:
  - `\r` on success, or
  - a value followed by `\r` for `V`, `N`, and `F`, or
  - a single `BEL` byte (`0x07`, written `\a`) on any error. No `CR` follows it.
- An empty line (a bare `\r`) gets `\r`.
- A line longer than 256 characters gets one `BEL` as soon as the 257th character arrives. The rest of the line, up to and including its terminator, is discarded without a further response.
- Received CAN frames are pushed to the host whenever they arrive. They are never inserted inside another line, but they can arrive between a command and its response. Classify each line by its first byte: `t T r R d D b B` is a received frame, `V N F` is a value response, a bare `\r` is a success response, and `\a` is an error. Match responses to commands in the order the commands were sent.
- Commands can be sent back to back without waiting for each response. The serial receive buffer holds 1024 bytes. Command responses are never dropped: when the serial transmit buffer is full of received frames, the firmware waits for space before it answers and reads no further commands meanwhile. Keep the unanswered command bytes below 1024, or bytes are lost and `F` bit 3 is set.

### Line endings

| Host sends | Effect                                                              |
| ---------- | ------------------------------------------------------------------- |
| `\r`       | Ends the command.                                                   |
| `\r\n`     | Ends the command; the `\n` is ignored.                              |
| `\n`       | Ends the command.                                                   |
| `\n\r`     | Two terminators: the command, then an empty line, so two responses. |

### State and defaults

The firmware keeps its state in RAM. It survives closing and reopening the serial port; only a reset or power cycle restores the defaults:

| Setting           | After reset             |
| ----------------- | ----------------------- |
| Channel           | Closed                  |
| Nominal bit rate  | 500 kbit/s (as `S6`)    |
| Data bit rate     | 2 Mbit/s (as `Y2`)      |
| Timestamps        | Off                     |

A host that connects to a board left open by a previous session must send `C` before it can change the bit rate.

## Configure and open the CAN channel

| Command  | Required state | Effect                                                                                                | Success reply |
| -------- | -------------- | ----------------------------------------------------------------------------------------------------- | ------------- |
| `S<n>\r` | Closed         | Set the nominal bit rate, `n` from `0` to `8` (table below).                                          | `\r`          |
| `Y<n>\r` | Closed         | Set the CAN FD data-phase bit rate, `n` from `0` to `5` (table below). `Y5` gets `BEL` on the C092RC. | `\r`          |
| `O\r`    | Closed         | Open the channel in normal mode. This is the letter `O`, not zero.                                    | `\r`          |
| `L\r`    | Closed         | Open the channel in listen-only mode.                                                                 | `\r`          |
| `C\r`    | Open           | Close the channel.                                                                                    | `\r`          |
| `Z<n>\r` | Closed         | `Z1` turns receive timestamps on, `Z0` turns them off.                                                | `\r`          |

Each command gets `BEL` in the wrong state, with extra characters, or with an argument out of range. A rejected `S` or `Y`, including one the CAN driver cannot configure, leaves the previous bit rate in place.

`O` and `L` work without a prior `S` or `Y`: the channel opens at the current bit rates, which are the defaults above unless changed. Both clear the latched `F` flags. Bit rates set with `S` and `Y` persist across `C` and `O` until reset.

The controller always opens in CAN FD mode. Classic CAN frames are sent and received unchanged, and the CAN FD frame commands work without a prior `Y`.

| Command | Nominal bit rate |
| ------- | ---------------: |
| `S0`    |       10 kbit/s  |
| `S1`    |       20 kbit/s  |
| `S2`    |       50 kbit/s  |
| `S3`    |      100 kbit/s  |
| `S4`    |      125 kbit/s  |
| `S5`    |      250 kbit/s  |
| `S6`    |      500 kbit/s  |
| `S7`    |      800 kbit/s  |
| `S8`    |        1 Mbit/s  |

| Command | Data-phase bit rate                                |
| ------- | -------------------------------------------------- |
| `Y0`    | 500 kbit/s                                         |
| `Y1`    | 1 Mbit/s                                           |
| `Y2`    | 2 Mbit/s                                           |
| `Y3`    | 3 Mbit/s                                           |
| `Y4`    | 4 Mbit/s                                           |
| `Y5`    | 5 Mbit/s on the H7S3L8. Always `BEL` on the C092RC |

The data-phase bit rate applies only to frames sent with bit-rate switching (`b`, `B`). `d` and `D` frames use the nominal bit rate throughout.

### Listen-only mode

A channel opened with `L` sends no ACKs and no frames, but receives everything on the bus. Transmit commands still get `\r`: the frame is queued in a transmit mailbox and stays there until `C`.

Receivers ignore the ACK slot. When the listener is the only other node, nobody ACKs a frame, the sender retransmits it, and the listener reports every retransmission as a new frame until the sender closes its channel.

## Transmit CAN frames

All frame commands require an open channel.

| Command              | Frame type                           | Identifier                             | DLC digit  |
| -------------------- | ------------------------------------ | -------------------------------------- | ---------- |
| `t<ID><DLC><DATA>\r` | Classic, standard data               | 3 hex digits, `000` to `7FF`           | `0` to `8` |
| `T<ID><DLC><DATA>\r` | Classic, extended data               | 8 hex digits, `00000000` to `1FFFFFFF` | `0` to `8` |
| `r<ID><DLC>\r`       | Classic, standard remote (RTR)       | 3 hex digits, `000` to `7FF`           | `0` to `8` |
| `R<ID><DLC>\r`       | Classic, extended remote (RTR)       | 8 hex digits, `00000000` to `1FFFFFFF` | `0` to `8` |
| `d<ID><DLC><DATA>\r` | CAN FD, standard, no bit-rate switch | 3 hex digits, `000` to `7FF`           | `0` to `F` |
| `D<ID><DLC><DATA>\r` | CAN FD, extended, no bit-rate switch | 8 hex digits, `00000000` to `1FFFFFFF` | `0` to `F` |
| `b<ID><DLC><DATA>\r` | CAN FD, standard, bit-rate switch    | 3 hex digits, `000` to `7FF`           | `0` to `F` |
| `B<ID><DLC><DATA>\r` | CAN FD, extended, bit-rate switch    | 8 hex digits, `00000000` to `1FFFFFFF` | `0` to `F` |

- Pad the identifier on the left with zeros to the full width.
- The DLC is one hex digit. For CAN FD, DLC values above 8 map to payload lengths as follows:

  | DLC   | `9` | `A` | `B` | `C` | `D` | `E` | `F` |
  | ----- | --: | --: | --: | --: | --: | --: | --: |
  | Bytes |  12 |  16 |  20 |  24 |  32 |  48 |  64 |

- Data frames carry exactly as many bytes as the DLC gives, each as two hex digits. Remote frames carry no data; their DLC is the requested length. There are no CAN FD remote frames.
- The line length must match exactly. Missing or extra characters get `BEL`.

| Example                        | Meaning                                                 |
| ------------------------------ | ------------------------------------------------------- |
| `t1232A55A\r`                  | Standard ID `0x123`, two data bytes: `0xA5`, `0x5A`.    |
| `t1230\r`                      | Standard ID `0x123`, no data bytes.                     |
| `T000001232A55A\r`             | Extended ID `0x123`, two data bytes: `0xA5`, `0x5A`.    |
| `r1232\r`                      | Standard RTR, ID `0x123`, requested length 2.           |
| `R000001232\r`                 | Extended RTR, ID `0x123`, requested length 2.           |
| `b1239` + 24 hex digits + `\r` | CAN FD with bit-rate switch, ID `0x123`, 12 data bytes. |

### Transmit response

`\r` means the frame was placed in one of the controller's **3 transmit mailboxes**. It does not mean the frame was sent or acknowledged on the bus. There is no later completion message.

`BEL` means the frame was not queued, because:

- the channel is closed,
- the command is malformed or the identifier is out of range,
- all 3 mailboxes are occupied (also sets `F` bit 1), or
- the controller is bus-off.

Frames leave the mailboxes in the order the host sent them, whatever their identifiers: the controller runs its mailboxes as a FIFO (`CONFIG_CAN_MCAN_TX_FIFO=y`). A frame that cannot be sent, for example because no node ACKs it, therefore also holds back the frames queued after it.

A frame that no node ACKs is retransmitted until a node ACKs it or the channel is closed with `C`. Meanwhile its transmit error counter rises until the controller is error passive (`F` bits 2 and 5), and the occupied mailboxes make later transmit commands get `BEL`. If the controller goes bus-off, it discards the frames in its mailboxes, sets `F` bit 7, and recovers automatically. `C` discards pending frames without setting a flag.

The firmware does not echo transmitted frames back to the host.

## Receive frames and timestamps

The firmware accepts every frame on the bus: standard and extended, classic and CAN FD, data and remote. There is no hardware filter.

Received frames use the transmit formats above. The prefix encodes the frame type:

| Frame                                  | Prefix (standard / extended) |
| -------------------------------------- | ---------------------------- |
| CAN FD with bit-rate switch            | `b` / `B`                    |
| CAN FD without bit-rate switch         | `d` / `D`                    |
| Classic remote                         | `r` / `R`                    |
| Classic data                           | `t` / `T`                    |

A classic frame received with a DLC above 8 is reported with DLC `8` and 8 data bytes.

Frames are forwarded only while the channel is open. Frames still waiting in the firmware when `C` is processed are discarded.

### Receive overflow

Received frames pass through a 32-frame queue and a 4096-byte serial transmit buffer. At 1 Mbaud the serial link carries about 100 kB/s, and a busy bus produces more than that, for example ~198 kB/s for classic 8-byte frames at 1 Mbit/s, or ~460 kB/s for 64-byte CAN FD frames at a 2 Mbit/s data rate. When either buffer is full, the firmware drops whole frames (never partial lines) and sets `F` bit 0.

### Timestamps

With timestamps on, the firmware appends four upper-case hex digits immediately before the final `CR` of each received frame. The suffix is not part of the payload and does not change the DLC.

- `t1232A55A\r`: a received standard data frame without a timestamp.
- `t1232A55A03E8\r`: the same frame with timestamp `0x03E8` (1,000 ms).

The value is the firmware uptime in milliseconds modulo 60,000, sampled when the frame arrives: it counts from `0000` to `EA5F` (59,999) and wraps to `0000`. It is not reset by `Z1`, `O`, or `L`. Command responses and transmit acknowledgments carry no timestamp.

The setting is kept in RAM only and resets to off. Send `Z1` after every reset.

## Read status and identify the adapter

| Command | Required state | Success reply                                                                  |
| ------- | -------------- | ------------------------------------------------------------------------------ |
| `F\r`   | Open           | `F<flags>\r`, where `<flags>` is one status byte as two upper-case hex digits. |
| `V\r`   | Any            | Always `V0102\r`: hardware field `01`, firmware field `02`.                    |
| `N\r`   | Any            | Always `NC092\r` on the C092RC, `NH7S3\r` on the H7S3L8.                       |

`F` gets `BEL` while the channel is closed. `V` and `N` ignore any characters after the command letter.

Decode the `F` value as a bitmask. All flags are latched: once set, they stay set until the next `F` read, `O`, or `L` clears them. In addition, bits 2, 5, and 7 are set in every reply for as long as the controller is in the matching state, so reading `F` does not clear them while the condition lasts.

| Bit | Mask   | Meaning                                                                                        |
| --: | ------ | ---------------------------------------------------------------------------------------------- |
|   0 | `0x01` | Received CAN frame dropped: the receive queue or the serial transmit buffer was full           |
|   1 | `0x02` | Transmit command rejected because all 3 mailboxes were occupied                                |
|   2 | `0x04` | Controller error warning state                                                                 |
|   3 | `0x08` | Serial input from the host dropped: the serial receive buffer was full or a DMA error occurred |
|   4 | `0x10` | Unused                                                                                         |
|   5 | `0x20` | Controller error passive state                                                                 |
|   6 | `0x40` | Arbitration lost (reported only in one-shot mode, which this firmware does not use)            |
|   7 | `0x80` | Bus-off, or a transmission failed with an error other than arbitration loss                    |

Example: `F22\r` after transmitting with no other node on the bus means the mailboxes filled up (bit 1) and the controller is error passive (bit 5).

## Unsupported commands

`s` (bit-timing registers), `M` and `m` (acceptance filters), `P` and `A` (polling), and every other letter not listed above get `BEL`.

## Compatibility with LAWICEL SLCAN hosts

The firmware aims to work with host software written for LAWICEL SLCAN adapters. The differences below can break such software.

### Protocol differences

| Area                    | LAWICEL SLCAN                                   | This firmware                                                                                                     | What can break                                                                                                                                                          |
| ----------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transmit acknowledgment | `z\r` for `t`/`r`, `Z\r` for `T`/`R`            | `\r` for every frame command                                                                                      | Hosts that wait for `z`/`Z` time out; hosts that count them to track queued frames miscount.                                                                            |
| Custom bit timing       | `s<BTR0><BTR1>` sets SJA1000 registers          | `BEL`                                                                                                             | Hosts that use custom timing. If the host ignores the `BEL` and sends `O`, the channel opens at the previous bit rate without an error.                                 |
| Acceptance filters      | `M` / `m` set hardware filters                  | `BEL`; every frame is forwarded                                                                                   | Hosts relying on hardware filtering get all bus traffic and must filter themselves; a busy bus can overflow the serial link (`F` bit 0).                                |
| Open without bit rate   | `O` requires a prior `S` or `s`                 | `O` opens at the current rate, 500 kbit/s after reset                                                             | A host that forgets `S` gets no error and may join the bus at the wrong rate, disturbing it with error frames.                                                          |
| Bit-rate table          | `S0`–`S8`                                       | Same; `S7` is 800 kbit/s; no other values                                                                         | Hosts that map 750 or 83.3 kbit/s to `S7`/`S9` get 800 kbit/s or `BEL`.                                                                                                 |
| Timestamp setting       | Stored persistently                             | RAM only, off after reset                                                                                         | Hosts that configure `Z1` once get no timestamps after the board resets.                                                                                                |
| Timestamp value         | Milliseconds, wraps at 60,000                   | Same, counted from firmware boot                                                                                  | None expected; the value is not aligned to `O` or `Z1`.                                                                                                                 |
| Transmit queue          | Adapter-specific buffer                         | 3 mailboxes, sent in FIFO order                                                                                   | Hosts that send without waiting for responses lose frames to `BEL` when the bus is slower than the host. A queued low-priority frame delays later high-priority frames. |
| State errors            | Vary by adapter                                 | `C` while closed and `O`/`L`/`S`/`Y`/`Z` while open get `BEL`                                                     | Hosts that send `C` defensively before configuring get a `BEL`; harmless unless they treat it as fatal.                                                                 |
| `F` availability        | Open channel                                    | Open channel only                                                                                                 | `F` sent before `O` gets `BEL`.                                                                                                                                         |
| `F` bits                | Bit 3 controller data overrun, bit 7 bus error  | Bit 3 serial input overrun, bit 7 bus-off or transmit failure, bit 6 never set, bits 2/5/7 reflect the live state | Hosts that decode bits 3, 6, and 7 with the controller meanings misreport errors; hosts that expect a read to clear bits 2, 5, or 7 see them again.                     |
| CAN FD                  | Not part of the protocol                        | `d`, `D`, `b`, `B`, `Y`, DLC `9`–`F`                                                                              | Classic-only hosts ignore received `d`/`D`/`b`/`B` lines, so they never see FD traffic.                                                                                 |
| Command parsing         | Varies                                          | Exact length; extra characters get `BEL`; lines over 256 characters get one `BEL`                                 | Hosts that append characters to frame commands or send data bytes with `r`/`R`.                                                                                         |
| Line endings            | `\r`                                            | `\r`, `\n`, or `\r\n`; `\n\r` counts as two lines                                                                 | Hosts that send `\n\r` receive two responses per command.                                                                                                               |
| Serial speed            | Adapter-specific, often 115,200 baud            | 1,000,000 baud fixed                                                                                              | Hosts that default to another speed see garbage or nothing.                                                                                                             |
| State/error messages    | None in LAWICEL; some hosts parse `s`/`e` lines | Not sent                                                                                                          | Hosts that track the bus state from such lines always see error active.                                                                                                 |

### Linux kernel `slcan` driver (`slcand`)

- Set the tty to 1,000,000 baud (`slcand -S 1000000`).
- When the interface comes up with a bit rate, the driver sends `C\rS<n>\r`, optionally `F\r`, then `O\r` or `L\r` (listen-only). The `C` and the `F` arrive while the channel is closed and get `BEL`. The driver ignores command responses, so this has no effect. Its bit-rate table matches `S0`–`S8`, including 800 kbit/s for `S7`.
- The driver does not wait for transmit responses. Frames rejected with `BEL` because the mailboxes are full are lost without any error on the interface. Watch `F` bit 1 through another channel or keep the transmit rate below the bus capacity.
- Its line buffer holds 32 bytes, enough for the longest classic line with a timestamp. It parses only `t`, `T`, `r`, `R` (and `s`/`e` state and error lines, which this firmware does not send). Received CAN FD lines are dropped; those longer than 32 bytes are counted as `rx_over_errors`.
- The interface state stays error active whatever the controller's state.

### python-can `slcan` interface

Checked against the python-can `main` branch.

- The default `tty_baudrate` is 115,200. Pass `tty_baudrate=1000000` or a channel such as `/dev/ttyACM0@1000000`.
- `bitrate=750000` sends `S7`, which is 800 kbit/s here. `bitrate=83300` sends `S9`, which gets `BEL`.
- `data_bitrate` accepts only 2 Mbit/s (`Y2`) and 5 Mbit/s (`Y5`). `Y5` works on the H7S3L8 and gets `BEL` on the C092RC. `timing=BitTiming` (or the deprecated `btr`) sends `s`, which gets `BEL`.
- python-can sends `C` before configuring and `O` (or `L` with `listen_only=True`) afterwards without reading the responses. A rejected `S`, `Y`, or `s` therefore opens the channel at the previous bit rates without an error.
- `send()` does not read responses. Frames rejected with `BEL` are lost silently.
- Received timestamps are ignored; python-can stamps each message with the host time.
- `get_version()` returns `(1, 2)`; `get_serial_number()` returns `"C092"` on the C092RC and `"H7S3"` on the H7S3L8.
