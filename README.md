# USB to I2C Bridge (ESP32-P4)

ESP32-P4 作为 USB CDC ACM 虚拟串口 ↔ I2C Master 通用桥接器。

## 开发环境

| 项目 | 说明 |
|------|------|
| **芯片** | ESP32-P4 (RISC-V) |
| **开发板** | ESP32-P4-Function-EV-Board |
| **ESP-IDF 版本** | v5.4.2 |
| **ESP-IDF 路径** | `C:\Users\jq163\esp\esp-idf` |
| **编译工具** | `C:\Users\jq163\esp\esp-idf\tools` |

## 板载存储配置

| 组件 | 型号 | 规格 | sdkconfig |
|------|------|------|-----------|
| **Flash** | GD25Q128ES1G | **16MB** (128Mbit) | `FLASHSIZE_16MB` |
| **PSRAM** | ESP32-P4NRW32 内置 | **4MB** (32Mbit) Octal PSRAM | `SPIRAM_MODE_HEX` @20MHz |
| **Flash 模式** | DIO | 80MHz | `FLASHMODE_DIO` |
| **堆分配** | PSRAM 优先 | <16KB 走内部 RAM | `SPIRAM_USE_MALLOC` |

## 硬件连接

```
ESP32-P4                    连接
┌──────────────────┐
│ USB_HS_DP ───────┼──► PC USB D+   (USB OTG HS, 内置 PHY)
│ USB_HS_DM ───────┼──► PC USB D-   (USB OTG HS, 内置 PHY)
│                  │
│ GPIO24 (D+) ─────┼──► PC USB D+   (USB Serial/JTAG, 调试/烧录)
│ GPIO25 (D-) ─────┼──► PC USB D-   (USB Serial/JTAG, 调试/烧录)
│                  │
│ GPIO7 (SDA) ─────┼──► I2C 设备 SDA
│ GPIO8 (SCL) ─────┼──► I2C 设备 SCL
│                  │
│ GND ─────────────┼──► I2C 设备 GND
└──────────────────┘
```

> **注意**: ESP32-P4 有两组独立的 USB PHY：
> - USB OTG HS: 数据通道，枚举为 CDC ACM 虚拟串口
> - USB Serial/JTAG: 独立调试通道，`ESP_LOG` 输出不污染数据通道

## 依赖

- ESP-IDF v5.4.2
- `espressif/esp_tinyusb ^1` (自动拉取)
- `espressif/tinyusb` (自动拉取，作为 esp_tinyusb 的依赖)

## sdkconfig 关键配置

```ini
# TinyUSB
CONFIG_TINYUSB_CDC_ENABLED=y
CONFIG_TINYUSB_CDC_RX_BUFSIZE=512
CONFIG_TINYUSB_CDC_TX_BUFSIZE=512
CONFIG_TINYUSB_RHPORT_HS=y
CONFIG_TINYUSB_MODE_DMA=y
CONFIG_TINYUSB_TASK_STACK_SIZE=8192

# Flash
CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y
CONFIG_SPIRAM_MODE_HEX=y

# Debug
CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y
```

## 编译与烧录

```bash
cd USBtoI2C

# 设置目标芯片
idf.py set-target esp32p4

# 配置 (可选: 修改 I2C 引脚、频率)
idf.py menuconfig

# 编译、烧录、查看日志
idf.py -p COM5 flash monitor
```

> COM5 = USB Serial/JTAG (烧录/调试日志), COM? = USB OTG HS CDC (数据)

## 通信协议

- **物理层**: USB CDC ACM (虚拟串口)
- **波特率**: 虚拟串口无需设置实际波特率，默认使用 115200
- **帧格式**: 文本命令，以 `\r\n` 或 `\n` 结尾
- **应答格式**: `OK: <data>\r\n` 或 `ERR: <message>\r\n`

## 命令列表

| 命令 | 说明 | 示例 |
|------|------|------|
| `I2C_SCAN` | 扫描 I2C 总线 (0x03-0x77) | `I2C_SCAN` |
| `I2C_PROBE <addr>` | 探测单个设备地址 | `I2C_PROBE 18` |
| `I2C_WR <dev> <reg> [d...]` | 写设备寄存器 | `I2C_WR 18 01 FF 00` |
| `I2C_RD <dev> <reg> <len>` | 读设备寄存器 | `I2C_RD 18 00 1` |
| `I2C_FREQ <hz>` | 设置 I2C 频率 | `I2C_FREQ 400000` |
| `INFO` | 查看桥接器信息 | `INFO` |
| `HELP` | 查看帮助 | `HELP` |

> 器件地址、寄存器地址、数据均为**十六进制** (可带或不带 `0x` 前缀)。
> 读取长度为**十进制**。

## PC 端使用

### Python GUI 测试工具

```powershell
python tools/i2c_test_tool.py
```
或直接运行 `tools/dist/USB-I2C-TestTool.exe`。

### Python 命令行

```bash
pip install pyserial

# 扫描总线
python tools/usb_i2c_client.py COM3 scan

# 向设备 0x18 的寄存器 0x01 写入 0xFF
python tools/usb_i2c_client.py COM3 wr 18 01 FF

# 从设备 0x18 的寄存器 0x00 读取 1 字节
python tools/usb_i2c_client.py COM3 rd 18 00 1

# 设置 I2C 频率为 400kHz
python tools/usb_i2c_client.py COM3 freq 400000
```

### 串口终端

```bash
# Linux
screen /dev/ttyACM0 115200
# 直接输入: I2C_SCAN

# Windows: 使用 Putty / Tera Term 打开 COM3
```

## 项目结构

```
USBtoI2C/
├── CMakeLists.txt
├── sdkconfig.defaults
├── README.md
├── main/
│   ├── CMakeLists.txt
│   ├── idf_component.yml
│   ├── Kconfig.projbuild
│   ├── usb_i2c_bridge.c          # 主程序 (初始化 USB CDC + I2C)
│   ├── cmd_parser.c/h            # 命令解析器 (环形缓冲区 + 行分割)
│   └── cmd_handler.c/h           # 命令处理器 (I2C 操作分发)
├── components/
│   └── i2c_utils/
│       ├── CMakeLists.txt
│       ├── i2c_utils.c            # I2C 读写/扫描/probe 工具函数
│       └── i2c_utils.h
└── tools/
    ├── usb_i2c_client.py          # PC 端 Python CLI 客户端
    ├── i2c_test_tool.py           # GUI 测试工具
    ├── example_script.i2c         # 示例脚本
    └── dist/
        └── USB-I2C-TestTool.exe   # 预编译 GUI 可执行文件
```

## 架构

```
PC ←─USB CDC ACM──→ ESP32-P4 ←─I2C Master──→ I2C 设备
     (虚拟串口)       (桥接器)    (SDA/SCL)      (任意)

命令: "I2C_WR 18 01 FF\r\n"
                     │
         ┌───────────▼───────────┐
         │  TinyUSB CDC RX Callback
         │  → cmd_parser_feed()
         │  → on_command("I2C_WR 18 01 FF")
         │  → cmd_handler_process()
         │  → i2c_utils_write_reg(0x18, 0x01, [0xFF])
         │  → i2c_master_transmit()
         └───────────┬───────────┘
                     │
回复: "OK: Write 0x18 reg 0x01 = FF\r\n"
```
