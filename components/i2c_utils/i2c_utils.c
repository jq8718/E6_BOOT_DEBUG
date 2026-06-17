/*
 * I2C Utility Functions - FINAL VERSION
 *
 * Uses ONLY legacy driver (driver/i2c.h) with i2c_master_cmd_begin.
 * Address bytes are manually shifted: (addr << 1) | R/W
 *
 * FIX: Probe only allows ACK from START+address (1 byte transfer).
 * Read/Write: full START+addr+reg+data+STOP sequence.
 * All operations use the SAME i2c_master_cmd_begin mechanism.
 */

#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "esp_log.h"
#include "esp_err.h"
#include "driver/i2c.h"
#include "i2c_utils.h"

#define I2C_XFER_TIMEOUT_MS  (50)
#define I2C_PORT             I2C_NUM_0

esp_err_t i2c_utils_init_bus(int port, gpio_num_t sda, gpio_num_t scl,
                              uint32_t freq_hz, i2c_master_bus_handle_t *bus)
{
    (void)port;
    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = sda,
        .scl_io_num = scl,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = freq_hz,
    };
    ESP_ERROR_CHECK(i2c_param_config(I2C_PORT, &conf));
    ESP_ERROR_CHECK(i2c_driver_install(I2C_PORT, I2C_MODE_MASTER, 0, 0, 0));
    *bus = (i2c_master_bus_handle_t)(intptr_t)I2C_PORT;
    ESP_LOGI("i2c", "OK SDA=%d SCL=%d", sda, scl);
    return ESP_OK;
}

esp_err_t i2c_utils_reconfig_bus(i2c_master_bus_handle_t *bus, gpio_num_t sda,
                                  gpio_num_t scl, uint32_t freq_hz)
{
    i2c_driver_delete(I2C_PORT);
    return i2c_utils_init_bus(-1, sda, scl, freq_hz, bus);
}

esp_err_t i2c_utils_deinit_bus(i2c_master_bus_handle_t bus)
{
    (void)bus;
    i2c_driver_delete(I2C_PORT);
    return ESP_OK;
}

/* U8 address -> wire byte: (addr << 1) | R/W
 * 7-bit addr 0x18 -> write 0x30, read 0x31 */
#define ADDR_W(addr)  ((uint8_t)(((addr) << 1) & 0xFE))
#define ADDR_R(addr)  ((uint8_t)(((addr) << 1) | 0x01))

/* Probe: START + addr(W) + STOP, check ACK */
esp_err_t i2c_utils_probe(i2c_master_bus_handle_t bus, uint8_t addr)
{
    (void)bus;
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, ADDR_W(addr), true);
    i2c_master_stop(cmd);
    esp_err_t ret = i2c_master_cmd_begin(I2C_PORT, cmd, I2C_XFER_TIMEOUT_MS);
    i2c_cmd_link_delete(cmd);
    return ret;
}

int i2c_utils_scan(i2c_master_bus_handle_t bus, char *output, size_t outlen)
{
    (void)bus;
    int n = 0, p = 0;
    p += snprintf(output+p, outlen-p, "     0  1  2  3  4  5  6  7  8  9  A  B  C  D  E  F\r\n");
    p += snprintf(output+p, outlen-p, "00: ");
    for (uint8_t a = 0x00; a <= 0x7F; a++) {
        if (a % 16 == 0) p += snprintf(output+p, outlen-p, "\r\n%02X: ", a);
        esp_err_t r = i2c_utils_probe(bus, a);
        if (r == ESP_OK)           { p += snprintf(output+p, outlen-p, "%02X ", a); n++; }
        else                           p += snprintf(output+p, outlen-p, "-- ");
    }
    p += snprintf(output+p, outlen-p, "\r\n%d found\r\n", n);
    return n;
}

/* Write: START + addr(W) + reg + data... + STOP */
esp_err_t i2c_utils_write_reg(i2c_master_bus_handle_t bus, uint8_t dev,
                               uint8_t reg, const uint8_t *data, size_t len)
{
    (void)bus;
    /* Buffer must be large enough for all command descriptors + data.
     * I2C_INTERNAL_STRUCT_SIZE=24, a write needs ~7 descriptors = 168 bytes.
     * Use dynamic allocation to be safe. */
    int remain = I2C_LINK_RECOMMENDED_SIZE(1 + len + 2);
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, ADDR_W(dev), true);
    i2c_master_write_byte(cmd, reg, true);
    for (size_t i = 0; i < len; i++) i2c_master_write_byte(cmd, data[i], true);
    i2c_master_stop(cmd);
    esp_err_t ret = i2c_master_cmd_begin(I2C_PORT, cmd, I2C_XFER_TIMEOUT_MS);
    i2c_cmd_link_delete(cmd);
    return ret;
}

esp_err_t i2c_utils_read_reg(i2c_master_bus_handle_t bus, uint8_t dev,
                              uint8_t reg, uint8_t *data, size_t len)
{
    (void)bus;
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, ADDR_W(dev), true);
    i2c_master_write_byte(cmd, reg, true);
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, ADDR_R(dev), true);
    if (len == 1) {
        i2c_master_read_byte(cmd, data, I2C_MASTER_NACK);
    } else {
        for (size_t i = 0; i < len - 1; i++)
            i2c_master_read_byte(cmd, &data[i], I2C_MASTER_ACK);
        i2c_master_read_byte(cmd, &data[len-1], I2C_MASTER_NACK);
    }
    i2c_master_stop(cmd);
    esp_err_t ret = i2c_master_cmd_begin(I2C_PORT, cmd, I2C_XFER_TIMEOUT_MS);
    i2c_cmd_link_delete(cmd);
    return ret;
}
