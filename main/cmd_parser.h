/*
 * Command Parser - Line-based text protocol parser
 *
 * Accumulates received bytes into a ring buffer, extracts complete lines
 * (delimited by \n or \r\n), and passes them to the handler.
 */
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Callback type for parsed command lines
 * @param line      The parsed command line (null-terminated, no newline)
 * @param response  Output buffer for response
 * @param resp_size Size of response buffer
 */
typedef void (*cmd_callback_t)(const char *line, char *response, size_t resp_size);

/**
 * @brief Initialize the command parser
 */
void cmd_parser_init(void);

/**
 * @brief Register the callback for processed commands
 */
void cmd_parser_set_callback(cmd_callback_t callback);

/**
 * @brief Feed received data into the parser
 *
 * May trigger one or more callback invocations when complete lines are found.
 *
 * @param data      Incoming raw data
 * @param len       Number of bytes
 * @param response  Output buffer for response (shared across calls)
 * @param resp_size Size of response buffer
 * @return true if at least one command was processed (response is ready)
 */
bool cmd_parser_feed(const uint8_t *data, size_t len, char *response, size_t resp_size);

#ifdef __cplusplus
}
#endif