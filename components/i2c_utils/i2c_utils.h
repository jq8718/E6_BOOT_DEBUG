/*
 * I2C Utility Functions
 * Generic I2C master operations: scan, probe, read register, write register
 */
#pragma once

#include <stdint.h>
#include <stddef.h>
#include "driver/i2c_master.h"
#include "esp_err.h"
#include "hal/i2c_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize I2C master bus
 *
 * @param port      I2C port number (-1 for auto)
 * @param sda       SDA GPIO number
 * @param scl       SCL GPIO number
 * @param freq_hz   I2C clock frequency in Hz
 * @param bus       Output bus handle
 * @return ESP_OK on success
 */
esp_err_t i2c_utils_init_bus(int port, gpio_num_t sda, gpio_num_t scl,
                              uint32_t freq_hz, i2c_master_bus_handle_t *bus);

/**
 * @brief Delete and re-create I2C bus with new frequency
 *
 * @param bus       Pointer to bus handle (old handle deleted, new one assigned)
 * @param sda       SDA GPIO
 * @param scl       SCL GPIO
 * @param freq_hz   New frequency
 * @return ESP_OK on success
 */
esp_err_t i2c_utils_reconfig_bus(i2c_master_bus_handle_t *bus, gpio_num_t sda,
                                  gpio_num_t scl, uint32_t freq_hz);

/**
 * @brief Probe a single I2C device address (7-bit)
 *
 * @param bus   I2C bus handle
 * @param addr  7-bit device address
 * @return ESP_OK if device ACKs, ESP_ERR_NOT_FOUND if no ACK
 */
esp_err_t i2c_utils_probe(i2c_master_bus_handle_t bus, uint8_t addr);

/**
 * @brief Scan all 7-bit I2C addresses (0x03-0x77)
 *
 * @param bus           I2C bus handle
 * @param output        Output string buffer
 * @param output_len    Buffer size
 * @return Number of devices found
 */
int i2c_utils_scan(i2c_master_bus_handle_t bus, char *output, size_t output_len);

/**
 * @brief Write data to an I2C device register
 *
 * Protocol: START + [dev_addr(W)] + [reg_addr] + [data...] + STOP
 *
 * @param bus       I2C bus handle
 * @param dev_addr  7-bit device address
 * @param reg_addr  Register / command byte
 * @param data      Data buffer to write
 * @param data_len  Number of data bytes
 * @return ESP_OK on success
 */
esp_err_t i2c_utils_write_reg(i2c_master_bus_handle_t bus, uint8_t dev_addr,
                               uint8_t reg_addr, const uint8_t *data,
                               size_t data_len);

/**
 * @brief Read data from an I2C device register
 *
 * Protocol: START + [dev_addr(W)] + [reg_addr] + RESTART + [dev_addr(R)] + [data...] + STOP
 *
 * @param bus       I2C bus handle
 * @param dev_addr  7-bit device address
 * @param reg_addr  Register / command byte to read from
 * @param data      Output buffer
 * @param data_len  Number of bytes to read
 * @return ESP_OK on success
 */
esp_err_t i2c_utils_read_reg(i2c_master_bus_handle_t bus, uint8_t dev_addr,
                              uint8_t reg_addr, uint8_t *data, size_t data_len);

/**
 * @brief Delete I2C bus
 */
esp_err_t i2c_utils_deinit_bus(i2c_master_bus_handle_t bus);

#ifdef __cplusplus
}
#endif