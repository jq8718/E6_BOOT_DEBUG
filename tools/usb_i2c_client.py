#!/usr/bin/env python3
"""
USB-I2C Bridge PC Client

Sends commands to the ESP32-P4 USB-I2C Bridge over a virtual COM port.
Can be used as both a CLI tool and an importable Python module.

Usage:
    python usb_i2c_client.py COM3 scan              # Scan I2C bus
    python usb_i2c_client.py COM3 probe 0x18         # Probe address 0x18
    python usb_i2c_client.py COM3 wr 0x18 0x01 FF    # Write 0xFF to reg 0x01 of device 0x18
    python usb_i2c_client.py COM3 rd 0x18 0x00 1     # Read 1 byte from reg 0x00 of device 0x18

Requires:
    pip install pyserial
"""

import sys
import time
import argparse

try:
    import serial
except ImportError:
    print("Error: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)


class I2CBridge:
    """Client for ESP32-P4 USB-I2C Bridge."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0):
        """
        Open serial connection to the bridge.

        Args:
            port: Serial port name (e.g., 'COM3' on Windows, '/dev/ttyACM0' on Linux)
            baudrate: Ignored for USB CDC ACM (virtual speed), but needed for serial config
            timeout: Read timeout in seconds
        """
        self.ser = serial.Serial(port, baudrate, timeout=timeout)
        time.sleep(0.1)  # Let the connection settle

    def close(self):
        """Close the serial connection."""
        self.ser.close()

    def send_cmd(self, cmd: str) -> str:
        """
        Send a command and return the response.

        Args:
            cmd: Command string (e.g., "I2C_SCAN")

        Returns:
            Response string from the bridge (without trailing newline)
        """
        payload = cmd.strip() + "\r\n"
        self.ser.write(payload.encode('utf-8'))
        self.ser.flush()
        response = self.ser.readline().decode('utf-8', errors='replace').strip()
        return response

    def scan(self) -> str:
        """Scan the I2C bus for devices."""
        return self.send_cmd("I2C_SCAN")

    def probe(self, addr: int) -> bool:
        """
        Probe a single I2C address.

        Args:
            addr: 7-bit I2C address (0-127)

        Returns:
            True if device found, False otherwise
        """
        resp = self.send_cmd(f"I2C_PROBE {addr:02X}")
        return "found" in resp.lower()

    def write_register(self, dev_addr: int, reg_addr: int, data: list[int]) -> str:
        """
        Write data to an I2C device register.

        Args:
            dev_addr: 7-bit I2C device address
            reg_addr: Register / command byte
            data: List of data bytes to write

        Returns:
            Response string
        """
        data_hex = " ".join(f"{b:02X}" for b in data)
        cmd = f"I2C_WR {dev_addr:02X} {reg_addr:02X}"
        if data_hex:
            cmd += " " + data_hex
        return self.send_cmd(cmd)

    def read_register(self, dev_addr: int, reg_addr: int, length: int) -> str:
        """
        Read data from an I2C device register.

        Args:
            dev_addr: 7-bit I2C device address
            reg_addr: Register / command byte
            length: Number of bytes to read (1-128)

        Returns:
            Response string containing the data
        """
        return self.send_cmd(f"I2C_RD {dev_addr:02X} {reg_addr:02X} {length}")

    def set_frequency(self, freq_hz: int) -> str:
        """Set I2C bus frequency."""
        return self.send_cmd(f"I2C_FREQ {freq_hz}")

    def info(self) -> str:
        """Get bridge info."""
        return self.send_cmd("INFO")

    def help(self) -> str:
        """Get help text."""
        return self.send_cmd("HELP")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def main():
    parser = argparse.ArgumentParser(
        description="ESP32-P4 USB-I2C Bridge Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python usb_i2c_client.py COM3 scan
  python usb_i2c_client.py COM3 probe 18
  python usb_i2c_client.py COM3 wr 18 01 FF
  python usb_i2c_client.py COM3 rd 18 00 1
  python usb_i2c_client.py COM3 freq 400000
  python usb_i2c_client.py COM3 info
        """
    )
    parser.add_argument("port", help="Serial port (e.g., COM3, /dev/ttyACM0)")
    parser.add_argument("-b", "--baud", type=int, default=115200,
                        help="Baud rate (default: 115200, ignored for USB CDC)")
    parser.add_argument("-t", "--timeout", type=float, default=2.0,
                        help="Read timeout in seconds (default: 2.0)")

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # I2C_SCAN
    subparsers.add_parser("scan", help="Scan I2C bus for devices")

    # I2C_PROBE
    probe_p = subparsers.add_parser("probe", help="Probe a single I2C address")
    probe_p.add_argument("addr", help="Device address in hex (e.g., 18, 0x18)")

    # I2C_WR
    wr_p = subparsers.add_parser("wr", help="Write to device register")
    wr_p.add_argument("dev", help="Device address in hex")
    wr_p.add_argument("reg", help="Register address in hex")
    wr_p.add_argument("data", nargs="+", help="Data bytes in hex")

    # I2C_RD
    rd_p = subparsers.add_parser("rd", help="Read from device register")
    rd_p.add_argument("dev", help="Device address in hex")
    rd_p.add_argument("reg", help="Register address in hex")
    rd_p.add_argument("length", type=int, help="Number of bytes to read")

    # I2C_FREQ
    freq_p = subparsers.add_parser("freq", help="Set I2C frequency")
    freq_p.add_argument("value", type=int, help="Frequency in Hz (10000-400000)")

    # INFO
    subparsers.add_parser("info", help="Show bridge information")

    # HELP
    subparsers.add_parser("help", help="Show bridge command help")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    def parse_hex(s: str) -> int:
        """Parse hex string with or without 0x prefix."""
        s = s.lower().replace("0x", "")
        return int(s, 16)

    with I2CBridge(args.port, args.baud, args.timeout) as bridge:
        if args.command == "scan":
            print("Scanning I2C bus...")
            print(bridge.scan())

        elif args.command == "probe":
            addr = parse_hex(args.addr)
            print(f"Probing address 0x{addr:02X}...")
            print(bridge.probe(addr))

        elif args.command == "wr":
            dev = parse_hex(args.dev)
            reg = parse_hex(args.reg)
            data = [parse_hex(b) for b in args.data]
            print(f"Writing to device 0x{dev:02X}, register 0x{reg:02X}...")
            print(bridge.write_register(dev, reg, data))

        elif args.command == "rd":
            dev = parse_hex(args.dev)
            reg = parse_hex(args.reg)
            print(f"Reading from device 0x{dev:02X}, register 0x{reg:02X}, {args.length} byte(s)...")
            print(bridge.read_register(dev, reg, args.length))

        elif args.command == "freq":
            print(f"Setting I2C frequency to {args.value} Hz...")
            print(bridge.set_frequency(args.value))

        elif args.command == "info":
            print(bridge.info())

        elif args.command == "help":
            print(bridge.help())


if __name__ == "__main__":
    main()
