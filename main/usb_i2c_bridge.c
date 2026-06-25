/*
 * USB to I2C Bridge - Main Application
 * esp_tinyusb wrapper API. I2C via legacy driver.
 */

#include <stdio.h>
#include <string.h>
#include "sdkconfig.h"
#include "esp_log.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tinyusb.h"
#include "tusb_cdc_acm.h"
#include "driver/i2c.h"
#include "driver/ledc.h"
#include "driver/gpio.h"
#include "i2c_utils.h"
#include "cmd_parser.h"
#include "cmd_handler.h"

#ifndef CONFIG_I2C_SDA_GPIO
#define CONFIG_I2C_SDA_GPIO  7
#endif
#ifndef CONFIG_I2C_SCL_GPIO
#define CONFIG_I2C_SCL_GPIO  8
#endif
#ifndef CONFIG_I2C_MASTER_FREQ_HZ
#define CONFIG_I2C_MASTER_FREQ_HZ  100000
#endif

static const char *TAG = "usb-i2c-bridge";
static i2c_master_bus_handle_t i2c_bus = NULL;

#define RESP_BUF_SIZE  2048
static char response_buf[RESP_BUF_SIZE];

void tinyusb_cdc_rx_callback(int itf, cdcacm_event_t *event)
{
    uint8_t buf[CONFIG_TINYUSB_CDC_RX_BUFSIZE];
    size_t rx_size = 0;
    tinyusb_cdcacm_read(itf, buf, sizeof(buf), &rx_size);
    if (rx_size == 0) return;
    memset(response_buf, 0, sizeof(response_buf));
    bool has_response = cmd_parser_feed(buf, rx_size, response_buf, sizeof(response_buf));
    if (has_response && response_buf[0] != '\0') {
        tinyusb_cdcacm_write_queue(itf, (uint8_t *)response_buf, strlen(response_buf));
        tinyusb_cdcacm_write_flush(itf, 0);
    }
}

void tinyusb_cdc_line_state_changed_callback(int itf, cdcacm_event_t *event)
{
    int dtr = event->line_state_changed_data.dtr;
    ESP_LOGI(TAG, "DTR=%d", dtr);
    if (dtr) {
        vTaskDelay(pdMS_TO_TICKS(50));
        const char *w = "USB-I2C Bridge ready\r\n";
        tinyusb_cdcacm_write_queue(itf, (uint8_t *)w, strlen(w));
        tinyusb_cdcacm_write_flush(itf, 0);
    }
}

static void on_command(const char *line, char *response, size_t resp_size)
{
    cmd_handler_process(line, response, resp_size);
}

static void init_nvs(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

static void init_i2c(void)
{
    ESP_ERROR_CHECK(i2c_utils_init_bus(-1, CONFIG_I2C_SDA_GPIO,
        CONFIG_I2C_SCL_GPIO, CONFIG_I2C_MASTER_FREQ_HZ, &i2c_bus));
}

static void init_usb(void)
{
    const tinyusb_config_t tusb_cfg = {
        .device_descriptor = NULL, .string_descriptor = NULL, .external_phy = false,
#if TUD_OPT_HIGH_SPEED
        .fs_configuration_descriptor = NULL, .hs_configuration_descriptor = NULL, .qualifier_descriptor = NULL,
#else
        .configuration_descriptor = NULL,
#endif
    };
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));

    tinyusb_config_cdcacm_t acm_cfg = {
        .usb_dev = TINYUSB_USBDEV_0, .cdc_port = TINYUSB_CDC_ACM_0,
        .rx_unread_buf_sz = 64, .callback_rx = &tinyusb_cdc_rx_callback,
        .callback_rx_wanted_char = NULL, .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL
    };
    ESP_ERROR_CHECK(tusb_cdc_acm_init(&acm_cfg));
    ESP_ERROR_CHECK(tinyusb_cdcacm_register_callback(
        TINYUSB_CDC_ACM_0, CDC_EVENT_LINE_STATE_CHANGED,
        &tinyusb_cdc_line_state_changed_callback));
}

/* 50 Hz square wave on GPIO27 via LEDC */
static void init_pwm(void)
{
    ledc_timer_config_t timer = {
        .speed_mode       = LEDC_LOW_SPEED_MODE,
        .duty_resolution  = LEDC_TIMER_13_BIT,
        .timer_num        = LEDC_TIMER_0,
        .freq_hz          = 50,
        .clk_cfg          = LEDC_USE_XTAL_CLK,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&timer));

    ledc_channel_config_t channel = {
        .gpio_num       = 27,
        .speed_mode     = LEDC_LOW_SPEED_MODE,
        .channel        = LEDC_CHANNEL_0,
        .timer_sel      = LEDC_TIMER_0,
        .duty           = 1 << 12,  /* 50% duty = 4096 / 8192 */
        .hpoint         = 0,
    };
    ESP_ERROR_CHECK(ledc_channel_config(&channel));
}

/* GPIO23 pull-up input */
static void init_gpio(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = 1ULL << 23,
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io_conf));
}

void app_main(void)
{
    init_nvs();
    init_i2c();
    cmd_parser_init();
    cmd_parser_set_callback(on_command);
    cmd_handler_init(i2c_bus, CONFIG_I2C_SDA_GPIO, CONFIG_I2C_SCL_GPIO);
    init_pwm();
    init_gpio();
    init_usb();
    while (1) vTaskDelay(pdMS_TO_TICKS(1000));
}
