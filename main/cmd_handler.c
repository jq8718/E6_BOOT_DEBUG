/*
 * Command Handler Implementation
 *
 * Parses text commands and dispatches to I2C utility functions.
 */

#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <ctype.h>
#include "esp_log.h"
#include "cmd_handler.h"
#include "i2c_utils.h"

static const char *TAG = "cmd_handler";

/* Global state, set once at init */
static i2c_master_bus_handle_t g_bus = NULL;
static int                    g_sda = 0;
static int                    g_scl = 0;
static int                    g_bus_initialized = 0; /* 1 after cmd_handler_init */

/* --------------------------------------------------------------------------
 * Initialization
 * -------------------------------------------------------------------------- */

void cmd_handler_init(i2c_master_bus_handle_t bus, int sda, int scl)
{
    g_bus = bus;
    g_sda = sda;
    g_scl = scl;
    g_bus_initialized = 1;
}

/* --------------------------------------------------------------------------
 * Helper: convert hex string to uint8_t
 * -------------------------------------------------------------------------- */

static bool parse_hex8(const char *s, uint8_t *val)
{
    char *end;
    unsigned long v = strtoul(s, &end, 16);
    if (end == s || *end != '\0' || v > 0xFF) {
        return false;
    }
    *val = (uint8_t)v;
    return true;
}

static bool parse_uint(const char *s, int *val)
{
    char *end;
    long v = strtol(s, &end, 10);
    if (end == s || *end != '\0' || v < 0) {
        return false;
    }
    *val = (int)v;
    return true;
}

/* --------------------------------------------------------------------------
 * Tokenizer helpers
 * -------------------------------------------------------------------------- */

/* Copy a delimited token (space-separated) into a buffer */
static void copy_token(const char *tok, char *out, size_t out_size)
{
    size_t i = 0;
    while (*tok && !isspace((unsigned char)*tok) && i < out_size - 1) {
        out[i++] = *tok++;
    }
    out[i] = '\0';
}

/* Case-insensitive string compare */
static bool streq_ci(const char *a, const char *b)
{
    while (*a && *b) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) return false;
        a++; b++;
    }
    return *a == *b;
}

/* Get the n-th token as a null-terminated copy */
static const char *get_token(const char *line, int n, char *buf, size_t buf_size)
{
    int tok_idx = 0;
    const char *p = line;

    while (*p) {
        while (*p && isspace((unsigned char)*p)) p++;
        if (!*p) break;

        if (tok_idx == n) {
            copy_token(p, buf, buf_size);
            return buf;
        }

        while (*p && !isspace((unsigned char)*p)) p++;
        tok_idx++;
    }
    return NULL;
}

/* --------------------------------------------------------------------------
 * I2C_FREQ handler
 * -------------------------------------------------------------------------- */

static void handle_freq(const char *line, char *response, size_t resp_size)
{
    char buf[32];
    const char *freq_str = get_token(line, 1, buf, sizeof(buf));
    if (!freq_str) {
        snprintf(response, resp_size, "ERR: Usage: I2C_FREQ <freq_hz> (e.g. 400000)\r\n");
        return;
    }
    int freq;
    if (!parse_uint(freq_str, &freq) || freq < 10000 || freq > 400000) {
        snprintf(response, resp_size, "ERR: Invalid frequency. Range: 10000-400000 Hz\r\n");
        return;
    }

    esp_err_t ret = i2c_utils_reconfig_bus(&g_bus, g_sda, g_scl, (uint32_t)freq);
    if (ret == ESP_OK) {
        snprintf(response, resp_size, "OK: I2C frequency set to %d Hz\r\n", freq);
    } else {
        snprintf(response, resp_size, "ERR: Failed to reconfigure bus: %s\r\n",
                 esp_err_to_name(ret));
    }
}

/* --------------------------------------------------------------------------
 * I2C_SCAN handler
 * -------------------------------------------------------------------------- */

static void handle_scan(char *response, size_t resp_size)
{
    int found = i2c_utils_scan(g_bus, response, resp_size);
    ESP_LOGI(TAG, "I2C scan: found %d device(s)", found);
}

/* --------------------------------------------------------------------------
 * I2C_PROBE handler
 * -------------------------------------------------------------------------- */

static void handle_probe(const char *line, char *response, size_t resp_size)
{
    char buf[32];
    const char *addr_str = get_token(line, 1, buf, sizeof(buf));
    if (!addr_str) {
        snprintf(response, resp_size, "ERR: Usage: I2C_PROBE <dev_addr_hex>\r\n");
        return;
    }
    uint8_t addr;
    if (!parse_hex8(addr_str, &addr)) {
        snprintf(response, resp_size, "ERR: Invalid address '%s'\r\n", addr_str);
        return;
    }

    esp_err_t ret = i2c_utils_probe(g_bus, addr);
    if (ret == ESP_OK) {
        snprintf(response, resp_size, "OK: Device 0x%02X found\r\n", addr);
    } else if (ret == ESP_ERR_TIMEOUT) {
        snprintf(response, resp_size, "OK: Device 0x%02X NOT found (timeout)\r\n", addr);
    } else {
        snprintf(response, resp_size, "ERR: Probe failed for 0x%02X: %s\r\n",
                 addr, esp_err_to_name(ret));
    }
}

/* --------------------------------------------------------------------------
 * I2C_WR handler
 * Format: I2C_WR <dev_addr> <reg_addr> [<byte0> <byte1> ...]
 * -------------------------------------------------------------------------- */

static void handle_write(const char *line, char *response, size_t resp_size)
{
    char dev_str_buf[32], reg_str_buf[32], tok_buf2[32];
    const char *dev_str  = get_token(line, 1, dev_str_buf, sizeof(dev_str_buf));
    const char *reg_str  = get_token(line, 2, reg_str_buf, sizeof(reg_str_buf));

    if (!dev_str || !reg_str) {
        snprintf(response, resp_size,
                 "ERR: Usage: I2C_WR <dev_addr_hex> <reg_hex> [<data_hex>...]\r\n");
        return;
    }

    uint8_t dev_addr, reg_addr;
    if (!parse_hex8(dev_str, &dev_addr)) {
        snprintf(response, resp_size, "ERR: Invalid device address '%s'\r\n", dev_str);
        return;
    }
    if (!parse_hex8(reg_str, &reg_addr)) {
        snprintf(response, resp_size, "ERR: Invalid register address '%s'\r\n", reg_str);
        return;
    }

    /* Collect data bytes from remaining tokens */
    static uint8_t data[540];
    size_t data_len = 0;
    for (int i = 3; ; i++) {
        const char *tok = get_token(line, i, tok_buf2, sizeof(tok_buf2));
        if (!tok) break;
        if (data_len >= sizeof(data)) {
            snprintf(response, resp_size, "ERR: Too many data bytes (max %zu)\r\n", sizeof(data));
            return;
        }
        uint8_t byte_val;
        if (!parse_hex8(tok, &byte_val)) {
            snprintf(response, resp_size, "ERR: Invalid data byte '%s'\r\n", tok);
            return;
        }
        data[data_len++] = byte_val;
    }

    esp_err_t ret = i2c_utils_write_reg(g_bus, dev_addr, reg_addr, data, data_len);
    if (ret == ESP_OK) {
        snprintf(response, resp_size, "OK: Write 0x%02X reg 0x%02X = %zu bytes\r\n",
                 dev_addr, reg_addr, data_len);
    } else {
        snprintf(response, resp_size, "ERR: I2C write failed: %s\r\n",
                 esp_err_to_name(ret));
    }
}

/* --------------------------------------------------------------------------
 * I2C_RD handler
 * Format: I2C_RD <dev_addr> <reg_addr> <len>
 * -------------------------------------------------------------------------- */

static void handle_read(const char *line, char *response, size_t resp_size)
{
    char dev_str_buf[32], reg_str_buf[32], len_str_buf[32];
    const char *dev_str  = get_token(line, 1, dev_str_buf, sizeof(dev_str_buf));
    const char *reg_str  = get_token(line, 2, reg_str_buf, sizeof(reg_str_buf));
    const char *len_str  = get_token(line, 3, len_str_buf, sizeof(len_str_buf));

    if (!dev_str || !reg_str || !len_str) {
        snprintf(response, resp_size,
                 "ERR: Usage: I2C_RD <dev_addr_hex> <reg_hex> <len_dec>\r\n");
        return;
    }

    uint8_t dev_addr, reg_addr;
    int read_len;

    if (!parse_hex8(dev_str, &dev_addr)) {
        snprintf(response, resp_size, "ERR: Invalid device address '%s'\r\n", dev_str);
        return;
    }
    if (!parse_hex8(reg_str, &reg_addr)) {
        snprintf(response, resp_size, "ERR: Invalid register address '%s'\r\n", reg_str);
        return;
    }
    if (!parse_uint(len_str, &read_len) || read_len < 1 || read_len > 530) {
        snprintf(response, resp_size, "ERR: Invalid read length '%s' (1-530)\r\n", len_str);
        return;
    }

    uint8_t data[540];
    esp_err_t ret = i2c_utils_read_reg(g_bus, dev_addr, reg_addr, data,
                                        (size_t)read_len);
    if (ret == ESP_OK) {
        int pos = snprintf(response, resp_size, "OK: Read 0x%02X reg 0x%02X =",
                           dev_addr, reg_addr);
        for (int i = 0; i < read_len && pos < (int)resp_size - 4; i++) {
            pos += snprintf(response + pos, resp_size - pos, " %02X", data[i]);
        }
        pos += snprintf(response + pos, resp_size - pos, "\r\n");
    } else {
        snprintf(response, resp_size, "ERR: I2C read failed: %s\r\n",
                 esp_err_to_name(ret));
    }
}

/* --------------------------------------------------------------------------
 * HELP handler
 * -------------------------------------------------------------------------- */

static void handle_help(char *response, size_t resp_size)
{
    snprintf(response, resp_size,
             "=== USB-I2C Bridge Commands ===\r\n"
             "  I2C_SCAN                     Scan bus for devices\r\n"
             "  I2C_PROBE <addr_hex>         Probe single address\r\n"
             "  I2C_WR <addr_hex> <reg_hex> [<data_hex> ...]\r\n"
             "                                Write up to 540 bytes to register\r\n"
             "  I2C_RD <addr_hex> <reg_hex> <len_dec>\r\n"
             "                                Read up to 530 bytes from register\r\n"
             "  I2C_FREQ <hz>                Set I2C clock (10000-400000)\r\n"
             "  INFO                         Bridge information\r\n"
             "  HELP                         This message\r\n"
             "\r\n"
             "  Address/register/data are in hex (e.g. 18, 0x18)\r\n"
             "  Length is in decimal\r\n");
}

/* --------------------------------------------------------------------------
 * INFO handler
 * -------------------------------------------------------------------------- */

static void handle_info(char *response, size_t resp_size)
{
    snprintf(response, resp_size,
             "=== USB-I2C Bridge Info ===\r\n"
             "  MCU:      ESP32-P4\r\n"
             "  SDK:      ESP-IDF v5.3.5\r\n"
             "  I2C SDA:  GPIO%d\r\n"
             "  I2C SCL:  GPIO%d\r\n"
             "  USB:      CDC ACM (TinyUSB)\r\n"
             "  Protocol: Text-based, lines delimited by \\n\r\n"
             "  Firmware: v1.0.0\r\n",
             g_sda, g_scl);
}

/* --------------------------------------------------------------------------
 * ECHO handler — pure USB round-trip latency test
 * -------------------------------------------------------------------------- */

static void handle_echo(const char *line, char *response, size_t resp_size)
{
    (void)line;
    size_t len = strlen(line);
    snprintf(response, resp_size, "OK: echo (%zu bytes)\r\n", len);
}

/* --------------------------------------------------------------------------
 * Main dispatch
 * -------------------------------------------------------------------------- */

void cmd_handler_process(const char *line, char *response, size_t resp_size)
{
    if (!line || !*line) {
        snprintf(response, resp_size, "ERR: Empty command\r\n");
        return;
    }

    /* Check if the bus is available (skip for HELP/INFO which don't need I2C) */
    if (!g_bus_initialized) {
        char cmd[32];
        get_token(line, 0, cmd, sizeof(cmd));
        if (!streq_ci(cmd, "HELP") && !streq_ci(cmd, "?") && !streq_ci(cmd, "INFO")) {
            snprintf(response, resp_size, "ERR: I2C bus not initialized\r\n");
            return;
        }
    }

    /* Get first token (command name) */
    char cmd[32];
    get_token(line, 0, cmd, sizeof(cmd));

    ESP_LOGI(TAG, "CMD: '%s'", line);

    if (streq_ci(cmd, "I2C_SCAN")) {
        handle_scan(response, resp_size);
    } else if (streq_ci(cmd, "I2C_PROBE")) {
        handle_probe(line, response, resp_size);
    } else if (streq_ci(cmd, "I2C_WR")) {
        handle_write(line, response, resp_size);
    } else if (streq_ci(cmd, "I2C_RD")) {
        handle_read(line, response, resp_size);
    } else if (streq_ci(cmd, "I2C_FREQ")) {
        handle_freq(line, response, resp_size);
    } else if (streq_ci(cmd, "HELP") || streq_ci(cmd, "?")) {
        handle_help(response, resp_size);
    } else if (streq_ci(cmd, "INFO")) {
        handle_info(response, resp_size);
    } else if (streq_ci(cmd, "ECHO")) {
        handle_echo(line, response, resp_size);
    } else if (streq_ci(cmd, "IAP_LATENCY")) {
        handle_echo(line, response, resp_size);
    } else {
        snprintf(response, resp_size,
                 "ERR: Unknown command '%s'. Type HELP for commands.\r\n", cmd);
    }
}