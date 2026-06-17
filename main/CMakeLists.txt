idf_component_register(
    SRCS "usb_i2c_bridge.c" "cmd_parser.c" "cmd_handler.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp_tinyusb driver nvs_flash i2c_utils
)
