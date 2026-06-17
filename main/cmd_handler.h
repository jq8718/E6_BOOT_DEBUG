/*
 * Command Handler
 *
 * Processes parsed command lines and executes I2C operations.
 * Commands:
 *   I2C_SCAN                  - Scan I2C bus
 *   I2C_PROBE <addr>          - Probe single device
 *   I2C_WR <addr> <reg> [d...] - Write register
 *   I2C_RD <addr> <reg> <len>  - Read register(s)
 *   I2C_FREQ <freq>            - Set I2C frequency
 *   HELP                       - Show help
 *   INFO                       - Show bridge info
 */
#pragma once

#include "driver/i2c_master.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize the command handler with the I2C bus
 *
 * @param bus   I2C master bus handle
 * @param sda   SDA GPIO (for reconfiguration)
 * @param scl   SCL GPIO (for reconfiguration)
 */
void cmd_handler_init(i2c_master_bus_handle_t bus, int sda, int scl);

/**
 * @brief Process a command line and write response
 *
 * @param line      Null-terminated command string
 * @param response  Output buffer for response
 * @param resp_size Buffer size
 */
void cmd_handler_process(const char *line, char *response, size_t resp_size);

#ifdef __cplusplus
}
#endif
