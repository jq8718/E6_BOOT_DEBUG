/*
 * Command Parser Implementation
 *
 * Simple line-based protocol parser with ring buffer.
 * Commands are delimited by \n or \r\n.
 */

#include <string.h>
#include <ctype.h>
#include "cmd_parser.h"

#define RX_BUF_SIZE  2048
#define RESP_BUF_SIZE 2048

static uint8_t  rx_buf[RX_BUF_SIZE];
static size_t   rx_head = 0;   /* Write position */
static size_t   rx_tail = 0;   /* Read position */
static size_t   rx_count = 0;

static cmd_callback_t user_callback = NULL;

void cmd_parser_init(void)
{
    rx_head = 0;
    rx_tail = 0;
    rx_count = 0;
    memset(rx_buf, 0, sizeof(rx_buf));
}

void cmd_parser_set_callback(cmd_callback_t callback)
{
    user_callback = callback;
}

/* --------------------------------------------------------------------------
 * Internal helpers
 * -------------------------------------------------------------------------- */

static inline bool buf_is_full(void)
{
    return rx_count >= RX_BUF_SIZE;
}

static inline bool buf_is_empty(void)
{
    return rx_count == 0;
}

static void buf_put(uint8_t byte)
{
    if (buf_is_full()) {
        /* Discard oldest byte to make room */
        rx_tail = (rx_tail + 1) % RX_BUF_SIZE;
        rx_count--;
    }
    rx_buf[rx_head] = byte;
    rx_head = (rx_head + 1) % RX_BUF_SIZE;
    rx_count++;
}

static uint8_t buf_peek(size_t offset)
{
    return rx_buf[(rx_tail + offset) % RX_BUF_SIZE];
}

static void buf_discard(size_t n)
{
    rx_tail = (rx_tail + n) % RX_BUF_SIZE;
    rx_count -= n;
}

/* Find the index of the first \n within the buffered data, returns -1 if none */
static int find_newline(void)
{
    for (size_t i = 0; i < rx_count; i++) {
        if (buf_peek(i) == '\n') {
            return (int)i;
        }
    }
    return -1;
}

/* Extract a line up to (but not including) the newline. Strips trailing \r. */
static size_t extract_line(char *out, size_t out_size, size_t line_len)
{
    size_t len = line_len;
    /* Strip trailing \r */
    if (len > 0 && buf_peek(len - 1) == '\r') {
        len--;
    }

    size_t copy_len = (len < out_size - 1) ? len : out_size - 1;
    for (size_t i = 0; i < copy_len; i++) {
        out[i] = (char)buf_peek(i);
    }
    out[copy_len] = '\0';

    /* Discard the line AND the newline character */
    buf_discard(line_len + 1);

    return copy_len;
}

/* Trim leading whitespace */
static char *trim_left(char *s)
{
    while (*s && isspace((unsigned char)*s)) s++;
    return s;
}

/* --------------------------------------------------------------------------
 * Public API
 * -------------------------------------------------------------------------- */

bool cmd_parser_feed(const uint8_t *data, size_t len, char *response, size_t resp_size)
{
    bool command_processed = false;
    size_t resp_total = 0;

    response[0] = '\0';

    for (size_t i = 0; i < len; i++) {
        buf_put(data[i]);
    }

    /* Process all complete lines, accumulating responses */
    while (1) {
        int nl_idx = find_newline();
        if (nl_idx < 0) break;

        if (nl_idx == 0) {
            /* Empty line - skip */
            buf_discard(1);
            continue;
        }

        static char line[2048];
        size_t line_len = extract_line(line, sizeof(line), (size_t)nl_idx);

        if (line_len == 0) continue;  /* Skip truly empty lines */

        char *cmd = trim_left(line);
        if (*cmd == '\0') continue;   /* Skip whitespace-only lines */

        if (user_callback) {
            static char line_resp[RESP_BUF_SIZE];
            user_callback(cmd, line_resp, sizeof(line_resp));

            /* Append line_resp to accumulated response */
            size_t line_resp_len = strlen(line_resp);
            if (resp_total + line_resp_len < resp_size - 1) {
                memcpy(response + resp_total, line_resp, line_resp_len);
                resp_total += line_resp_len;
                response[resp_total] = '\0';
            }
            command_processed = true;
        }
    }

    return command_processed;
}
