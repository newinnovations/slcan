#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/can.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/ring_buffer.h>
#include <string.h>
#include <stdbool.h>

/* Automatically resolves to fdcan1 and usart2 on nucleo_c092rc */
static const struct device *const can_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_canbus));
static const struct device *const uart_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

/* Ring buffers sized for 64-byte CAN FD ASCII bursts */
#define UART_RX_BUF_SIZE 1024
#define UART_TX_BUF_SIZE 4096
RING_BUF_DECLARE(uart_rx_rb, UART_RX_BUF_SIZE);
RING_BUF_DECLARE(uart_tx_rb, UART_TX_BUF_SIZE);

/* Message queue for incoming CAN / CAN FD frames */
CAN_MSGQ_DEFINE(can_rx_msgq, 32);

/* LAWICEL status flags reported (and cleared) by the 'F' command */
#define SLCAN_F_RX_QUEUE_FULL BIT(0) /* received CAN frame dropped before reaching host */
#define SLCAN_F_TX_QUEUE_FULL BIT(1) /* no free CAN TX mailbox */
#define SLCAN_F_ERROR_WARNING BIT(2)
#define SLCAN_F_DATA_OVERRUN  BIT(3) /* serial input from host dropped */
#define SLCAN_F_ERROR_PASSIVE BIT(5)
#define SLCAN_F_ARB_LOST      BIT(6)
#define SLCAN_F_BUS_ERROR     BIT(7)

/* Latched flags, set from ISR and thread context */
static atomic_t slcan_status;

static bool can_is_open = false;
static bool can_fd_enabled = false;
static uint32_t current_bitrate = 500000;
static uint32_t current_data_bitrate = 2000000;

/* Standard LAWICEL nominal bitrates (S0 - S8) */
static const uint32_t slcan_bitrates[] = {
    10000,   /* S0: 10 kbit/s */
    20000,   /* S1: 20 kbit/s */
    50000,   /* S2: 50 kbit/s */
    100000,  /* S3: 100 kbit/s */
    125000,  /* S4: 125 kbit/s */
    250000,  /* S5: 250 kbit/s */
    500000,  /* S6: 500 kbit/s */
    800000,  /* S7: 800 kbit/s */
    1000000  /* S8: 1 Mbit/s */
};

/* CAN FD data phase bitrates (Y0 - Y5) */
static const uint32_t slcan_data_bitrates[] = {
    500000,  /* Y0: 500 kbit/s */
    1000000, /* Y1: 1 Mbit/s */
    2000000, /* Y2: 2 Mbit/s */
    3000000, /* Y3: 3 Mbit/s */
    4000000, /* Y4: 4 Mbit/s */
    5000000  /* Y5: 5 Mbit/s */
};

/* ---------------- UART Helpers & ISR ---------------- */

static void uart_isr(const struct device *dev, void *user_data)
{
    ARG_UNUSED(user_data);

    while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
        if (uart_irq_rx_ready(dev)) {
            uint8_t buf[64];
            int len = uart_fifo_read(dev, buf, sizeof(buf));
            if (len > 0 && ring_buf_put(&uart_rx_rb, buf, len) < (uint32_t)len) {
                atomic_or(&slcan_status, SLCAN_F_DATA_OVERRUN);
            }
        }

        if (uart_irq_tx_ready(dev)) {
            uint8_t *data;
            uint32_t len = ring_buf_get_claim(&uart_tx_rb, &data, UART_TX_BUF_SIZE);
            int sent = 0;
            if (len > 0) {
                /* May accept fewer bytes than offered; the rest stays queued */
                sent = uart_fifo_fill(dev, data, len);
            } else {
                uart_irq_tx_disable(dev);
            }
            ring_buf_get_finish(&uart_tx_rb, sent > 0 ? (uint32_t)sent : 0U);
        }
    }
}

/* All-or-nothing so the host never receives a truncated SLCAN line */
static bool uart_try_send(const char *str, size_t len)
{
    if (ring_buf_space_get(&uart_tx_rb) < len) {
        return false;
    }
    ring_buf_put(&uart_tx_rb, (const uint8_t *)str, len);
    uart_irq_tx_enable(uart_dev);
    return true;
}

/* Command responses must not be lost: wait for the ISR to drain the buffer.
 * A full buffer is non-empty, so the TX interrupt is enabled and draining.
 */
static void uart_send_str(const char *str, size_t len)
{
    while (!uart_try_send(str, len)) {
        k_sleep(K_USEC(100));
    }
}

static inline void slcan_ack(void)
{
    uart_send_str("\r", 1);
}

static inline void slcan_nack(void)
{
    uart_send_str("\a", 1); /* ASCII BEL (0x07) */
}

/* ---------------- Hex Conversion Helpers ---------------- */

static int hex_char_to_val(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static char val_to_hex_char(uint8_t val)
{
    val &= 0x0F;
    return (val < 10) ? ('0' + val) : ('A' + val - 10);
}

static bool parse_hex_u32(const char *str, size_t digits, uint32_t *out)
{
    uint32_t res = 0;
    for (size_t i = 0; i < digits; i++) {
        int v = hex_char_to_val(str[i]);
        if (v < 0) return false;
        res = (res << 4) | (uint32_t)v;
    }
    *out = res;
    return true;
}

/* ---------------- CAN Callbacks ---------------- */

static atomic_val_t can_state_flags(enum can_state state)
{
    switch (state) {
    case CAN_STATE_ERROR_WARNING:
        return SLCAN_F_ERROR_WARNING;
    case CAN_STATE_ERROR_PASSIVE:
        return SLCAN_F_ERROR_PASSIVE;
    case CAN_STATE_BUS_OFF:
        return SLCAN_F_BUS_ERROR;
    default:
        return 0;
    }
}

static void can_state_cb(const struct device *dev, enum can_state state,
             struct can_bus_err_cnt err_cnt, void *user_data)
{
    ARG_UNUSED(dev);
    ARG_UNUSED(err_cnt);
    ARG_UNUSED(user_data);

    atomic_or(&slcan_status, can_state_flags(state));
}

static void can_rx_cb(const struct device *dev, struct can_frame *frame, void *user_data)
{
    ARG_UNUSED(dev);
    ARG_UNUSED(user_data);

    if (k_msgq_put(&can_rx_msgq, frame, K_NO_WAIT) != 0) {
        atomic_or(&slcan_status, SLCAN_F_RX_QUEUE_FULL);
    }
}

static void can_tx_cb(const struct device *dev, int error, void *user_data)
{
    ARG_UNUSED(dev);
    ARG_UNUSED(user_data);

    switch (error) {
    case 0:
    case -ENETDOWN: /* aborted by 'C' */
        break;
    case -EBUSY:
        atomic_or(&slcan_status, SLCAN_F_ARB_LOST);
        break;
    default:
        atomic_or(&slcan_status, SLCAN_F_BUS_ERROR);
        break;
    }
}

/* ---------------- SLCAN Command Parser ---------------- */

static void handle_tx_frame(const char *cmd, size_t len,
                bool is_ext, bool is_rtr, bool is_fdf, bool is_brs)
{
    if (!can_is_open) {
        slcan_nack();
        return;
    }

    size_t id_digits = is_ext ? 8 : 3;
    if (len < (1 + id_digits + 1)) {
        slcan_nack();
        return;
    }

    uint32_t id = 0;
    if (!parse_hex_u32(&cmd[1], id_digits, &id)) {
        slcan_nack();
        return;
    }

    int dlc = hex_char_to_val(cmd[1 + id_digits]);
    if (dlc < 0 || (!is_fdf && dlc > 8) || (is_fdf && dlc > 15)) {
        slcan_nack();
        return;
    }

    uint8_t data_len = can_dlc_to_bytes((uint8_t)dlc);

    struct can_frame frame = {0};
    frame.id = id;
    frame.dlc = (uint8_t)dlc;
    if (is_ext) frame.flags |= CAN_FRAME_IDE;
    if (is_rtr) frame.flags |= CAN_FRAME_RTR;
    if (is_fdf) frame.flags |= CAN_FRAME_FDF;
    if (is_brs) frame.flags |= CAN_FRAME_BRS;

    if (!is_rtr) {
        size_t expected_len = 1 + id_digits + 1 + (data_len * 2);
        if (len < expected_len) {
            slcan_nack();
            return;
        }
        for (uint8_t i = 0; i < data_len; i++) {
            uint32_t byte_val = 0;
            if (!parse_hex_u32(&cmd[1 + id_digits + 1 + (i * 2)], 2, &byte_val)) {
                slcan_nack();
                return;
            }
            frame.data[i] = (uint8_t)byte_val;
        }
    }

    /* Asynchronous: a synchronous send blocks forever if no node ACKs the frame.
     * ACK means the frame was queued in a TX mailbox; the outcome is reported via 'F'.
     */
    int err = can_send(can_dev, &frame, K_NO_WAIT, can_tx_cb, NULL);
    if (err == 0) {
        slcan_ack();
    } else {
        if (err == -EAGAIN) {
            atomic_or(&slcan_status, SLCAN_F_TX_QUEUE_FULL);
        }
        slcan_nack();
    }
}

static void open_can_channel(can_mode_t base_mode)
{
    can_mode_t mode = base_mode;
    if (can_fd_enabled) {
        mode |= CAN_MODE_FD;
    }

    if (can_set_mode(can_dev, mode) != 0) {
        slcan_nack();
        return;
    }

    if (can_start(can_dev) == 0) {
        can_is_open = true;
        atomic_clear(&slcan_status);
        slcan_ack();
    } else {
        slcan_nack();
    }
}

static void process_slcan_command(const char *cmd, size_t len)
{
    if (len == 0) {
        slcan_ack();
        return;
    }

    switch (cmd[0]) {
    case 'S': /* Set nominal bitrate S0..S8 */
        if (can_is_open || len != 2 || cmd[1] < '0' || cmd[1] > '8') {
            slcan_nack();
        } else {
            current_bitrate = slcan_bitrates[cmd[1] - '0'];
            if (can_set_bitrate(can_dev, current_bitrate) == 0) {
                slcan_ack();
            } else {
                slcan_nack();
            }
        }
        break;

    case 'Y': /* Set CAN FD data phase bitrate Y0..Y5 */
        if (can_is_open || len != 2 || cmd[1] < '0' || cmd[1] > '5') {
            slcan_nack();
        } else {
            current_data_bitrate = slcan_data_bitrates[cmd[1] - '0'];
            if (can_set_bitrate_data(can_dev, current_data_bitrate) == 0) {
                can_fd_enabled = true;
                slcan_ack();
            } else {
                slcan_nack();
            }
        }
        break;

    case 'O': /* Open CAN channel in normal mode */
        if (can_is_open || len != 1) {
            slcan_nack();
        } else {
            open_can_channel(CAN_MODE_NORMAL);
        }
        break;

    case 'L': /* Open CAN channel in listen-only (silent) mode */
        if (can_is_open || len != 1) {
            slcan_nack();
        } else {
            open_can_channel(CAN_MODE_LISTENONLY);
        }
        break;

    case 'C': /* Close CAN channel */
        if (!can_is_open || len != 1) {
            slcan_nack();
        } else {
            if (can_stop(can_dev) == 0) {
                can_is_open = false;
                slcan_ack();
            } else {
                slcan_nack();
            }
        }
        break;

    case 't': /* Classic CAN standard 11-bit frame */
        handle_tx_frame(cmd, len, false, false, false, false);
        break;

    case 'T': /* Classic CAN extended 29-bit frame */
        handle_tx_frame(cmd, len, true, false, false, false);
        break;

    case 'r': /* Classic CAN standard 11-bit RTR frame */
        handle_tx_frame(cmd, len, false, true, false, false);
        break;

    case 'R': /* Classic CAN extended 29-bit RTR frame */
        handle_tx_frame(cmd, len, true, true, false, false);
        break;

    case 'd': /* CAN FD standard 11-bit frame (no BRS) */
        handle_tx_frame(cmd, len, false, false, true, false);
        break;

    case 'D': /* CAN FD extended 29-bit frame (no BRS) */
        handle_tx_frame(cmd, len, true, false, true, false);
        break;

    case 'b': /* CAN FD standard 11-bit frame (with BRS) */
        handle_tx_frame(cmd, len, false, false, true, true);
        break;

    case 'B': /* CAN FD extended 29-bit frame (with BRS) */
        handle_tx_frame(cmd, len, true, false, true, true);
        break;

    case 'V': /* Hardware/Firmware version */
        uart_send_str("V0102\r", 6);
        break;

    case 'N': /* Serial number */
        uart_send_str("NC092\r", 6);
        break;

    case 'F': /* Read and clear status flags (channel must be open) */
        if (!can_is_open || len != 1) {
            slcan_nack();
        } else {
            enum can_state state;
            atomic_val_t flags = atomic_clear(&slcan_status);
            if (can_get_state(can_dev, &state, NULL) == 0) {
                flags |= can_state_flags(state);
            }
            const char resp[4] = {
                'F', val_to_hex_char((uint8_t)(flags >> 4)), val_to_hex_char((uint8_t)flags), '\r'
            };
            uart_send_str(resp, sizeof(resp));
        }
        break;

    case 'Z': /* Timestamp toggle (stubbed ACK) */
        slcan_ack();
        break;

    default:
        slcan_nack();
        break;
    }
}

/* ---------------- CAN RX Formatter ---------------- */

static void format_and_send_can_frame(const struct can_frame *frame)
{
    /* Max size: 'B' (1) + ID (8) + DLC (1) + 64 bytes hex (128) + '\r' (1) = 139 bytes */
    char out[144];
    size_t pos = 0;

    bool is_ext = (frame->flags & CAN_FRAME_IDE) != 0;
    bool is_rtr = (frame->flags & CAN_FRAME_RTR) != 0;
    bool is_fdf = (frame->flags & CAN_FRAME_FDF) != 0;
    bool is_brs = (frame->flags & CAN_FRAME_BRS) != 0;

    char prefix;
    if (is_fdf) {
        if (is_brs) {
            prefix = is_ext ? 'B' : 'b';
        } else {
            prefix = is_ext ? 'D' : 'd';
        }
    } else {
        if (is_rtr) {
            prefix = is_ext ? 'R' : 'r';
        } else {
            prefix = is_ext ? 'T' : 't';
        }
    }
    out[pos++] = prefix;

    if (is_ext) {
        for (int i = 7; i >= 0; i--) {
            out[pos++] = val_to_hex_char((frame->id >> (i * 4)) & 0x0F);
        }
    } else {
        for (int i = 2; i >= 0; i--) {
            out[pos++] = val_to_hex_char((frame->id >> (i * 4)) & 0x0F);
        }
    }

    uint8_t max_dlc = is_fdf ? 15 : 8;
    uint8_t dlc = (frame->dlc > max_dlc) ? max_dlc : frame->dlc;
    out[pos++] = val_to_hex_char(dlc);

    if (!is_rtr) {
        uint8_t data_len = can_dlc_to_bytes(dlc);
        for (uint8_t i = 0; i < data_len; i++) {
            out[pos++] = val_to_hex_char(frame->data[i] >> 4);
            out[pos++] = val_to_hex_char(frame->data[i] & 0x0F);
        }
    }

    out[pos++] = '\r';
    /* Drop whole frames when the serial link cannot keep up with the bus */
    if (!uart_try_send(out, pos)) {
        atomic_or(&slcan_status, SLCAN_F_RX_QUEUE_FULL);
    }
}

/* ---------------- Main Entry ---------------- */

int main(void)
{
    if (!device_is_ready(can_dev) || !device_is_ready(uart_dev)) {
        return -1;
    }

    uart_irq_callback_user_data_set(uart_dev, uart_isr, NULL);
    uart_irq_rx_enable(uart_dev);

    /* Accept all standard and extended Classic CAN + CAN FD frames */
    const struct can_filter std_filter = {
        .id = 0,
        .mask = 0,
        .flags = 0
    };
    const struct can_filter ext_filter = {
        .id = 0,
        .mask = 0,
        .flags = CAN_FILTER_IDE
    };

    if (can_add_rx_filter(can_dev, can_rx_cb, NULL, &std_filter) < 0 ||
        can_add_rx_filter(can_dev, can_rx_cb, NULL, &ext_filter) < 0) {
        return -1;
    }
    can_set_state_change_callback(can_dev, can_state_cb, NULL);

    /* Set default nominal (500 kbps) and data (2 Mbps) bitrates */
    can_set_bitrate(can_dev, current_bitrate);
    can_set_bitrate_data(can_dev, current_data_bitrate);

    char cmd_buf[256];
    size_t cmd_len = 0;
    struct can_frame rx_frame;

    while (1) {

        /* 1. Drain incoming UART bytes and assemble '\r'-terminated SLCAN commands */
        uint8_t c;
        while (ring_buf_get(&uart_rx_rb, &c, 1) == 1) {
            if (c == '\r' || c == '\n') {
                cmd_buf[cmd_len] = '\0';
                process_slcan_command(cmd_buf, cmd_len);
                cmd_len = 0;
            } else if (cmd_len < (sizeof(cmd_buf) - 1)) {
                cmd_buf[cmd_len++] = (char)c;
            } else {
                cmd_len = 0;
                slcan_nack();
            }
        }

        /* 2. Forward any received CAN frames to UART */
        while (k_msgq_get(&can_rx_msgq, &rx_frame, K_NO_WAIT) == 0) {
            if (can_is_open) {
                format_and_send_can_frame(&rx_frame);
            }

        }

        /* Yield briefly to avoid burning 100% CPU while idle */
        k_sleep(K_USEC(100));
    }
    return 0;
}
