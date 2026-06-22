#!/usr/bin/env python3
"""
I2C IAP Bootloader protocol helper for HC32L021.
Talks to the USB-I2C bridge using the existing text commands.
"""

import re
import time
import threading


class IapError(Exception):
    pass


class IapProtocol:
    # Virtual registers
    REG_STATUS = 0x00
    REG_ERROR = 0x01
    REG_CTRL = 0x02
    REG_TX_LEN = 0x06
    REG_MAILBOX = 0x20

    # CTRL values
    CTRL_COMMIT = 0xA5
    CTRL_CLEAR = 0x5A
    CTRL_ABORT = 0xC3

    # STATUS values
    ST_IDLE = 0x00
    ST_BUSY = 0x02
    ST_RESP_READY = 0x03
    ST_ERROR = 0x04

    # Commands
    CMD_HANDSHAKE = 0x20
    CMD_JUMP_TO_APP = 0x21
    CMD_JUMP_TO_BOOT = 0x23
    CMD_APP_DOWNLOAD = 0x22
    CMD_ERASE_FLASH = 0x24
    CMD_CRC_FLASH = 0x25

    def __init__(self, bridge, dev_addr, app_addr, log_callback=None):
        self.bridge = bridge
        self.dev = int(dev_addr) & 0x7F
        self.app_addr = int(app_addr) & 0xFFFFFFFF
        self.seq = 0
        self.log = log_callback or (lambda *_a, **_k: None)

    # ------------------------------------------------------------------
    # CRC16: init 0xA28C, reflected poly 0x8408, output inverted
    # ------------------------------------------------------------------
    @staticmethod
    def crc16(data: bytes) -> int:
        crc = 0xA28C
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0x8408
                else:
                    crc >>= 1
        return (~crc) & 0xFFFF

    # ------------------------------------------------------------------
    # Frame helpers
    # ------------------------------------------------------------------
    def build_frame(self, cmd, payload: bytes) -> bytes:
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFF
        header = bytes([
            0x6D, 0xAC, 0x01, cmd, seq, 0x00
        ]) + len(payload).to_bytes(2, 'little')
        body = header + payload
        crc = self.crc16(body)
        return body + crc.to_bytes(2, 'little')

    @staticmethod
    def parse_frame(buf: bytes):
        if len(buf) < 10:
            raise IapError(f"response too short: {len(buf)}")
        magic = buf[0:2]
        version = buf[2]
        cmd = buf[3]
        seq = buf[4]
        flags = buf[5]
        plen = int.from_bytes(buf[6:8], 'little')
        payload = buf[8:8 + plen]
        crc_recv = int.from_bytes(buf[8 + plen:10 + plen], 'little')
        crc_calc = IapProtocol.crc16(buf[:8 + plen])
        if magic != b'\x6d\xac':
            raise IapError("bad magic")
        if version != 0x01:
            raise IapError("bad version")
        if flags != 0x00:
            raise IapError("bad flags")
        if crc_recv != crc_calc:
            raise IapError(f"crc mismatch recv={crc_recv:04X} calc={crc_calc:04X}")
        return cmd, seq, payload

    # ------------------------------------------------------------------
    # Bridge-level I2C helpers
    # ------------------------------------------------------------------
    def _wr(self, reg, data: bytes):
        """I2C write via bridge text command."""
        dh = " ".join(f"{b:02X}" for b in data)
        cmd = f"I2C_WR {self.dev:02X} {reg:02X} {dh}"
        self.log("tx", cmd[:120] + ("..." if len(cmd) > 120 else ""))
        t0 = time.time()
        r = self.bridge.send_cmd(cmd)
        dt = (time.time() - t0) * 1000.0
        self.log("rx", f"{r.strip()}  [{dt:.1f}ms]")
        if not r.strip().startswith("OK"):
            raise IapError(f"I2C_WR failed: {r.strip()}")

    def _rd(self, reg, length: int) -> bytes:
        """I2C read via bridge text command."""
        cmd = f"I2C_RD {self.dev:02X} {reg:02X} {length}"
        self.log("tx", cmd)
        t0 = time.time()
        r = self.bridge.send_cmd(cmd)
        dt = (time.time() - t0) * 1000.0
        self.log("rx", f"{r.strip()}  [{dt:.1f}ms]")
        m = re.search(r"=\s*([0-9A-Fa-f ]*)$", r.strip())
        if not m:
            raise IapError(f"I2C_RD parse failed: {r.strip()}")
        vals = [int(x, 16) for x in m.group(1).strip().split() if x]
        if len(vals) != length:
            raise IapError(f"I2C_RD length mismatch: want {length} got {len(vals)}")
        return bytes(vals)

    # ------------------------------------------------------------------
    # Virtual register helpers
    # ------------------------------------------------------------------
    def read_status(self) -> int:
        return self._rd(self.REG_STATUS, 1)[0]

    def read_error(self) -> int:
        return self._rd(self.REG_ERROR, 1)[0]

    def read_tx_len(self) -> int:
        return int.from_bytes(self._rd(self.REG_TX_LEN, 2), 'little')

    def write_ctrl(self, val: int):
        self._wr(self.REG_CTRL, bytes([val]))

    def write_mailbox(self, frame: bytes):
        self._wr(self.REG_MAILBOX, frame)

    def read_mailbox(self, length: int) -> bytes:
        return self._rd(self.REG_MAILBOX, length)

    # ------------------------------------------------------------------
    # Standard command transaction
    # ------------------------------------------------------------------
    def transaction(self, cmd, payload: bytes, poll_timeout=10.0, busy_retry=None):
        frame = self.build_frame(cmd, payload)

        # Flash-less commands respond immediately; use tighter polling.
        if busy_retry is None:
            busy_retry = 0.005

        # 1. wait IDLE
        t0 = time.time()
        while True:
            st = self.read_status()
            if st == self.ST_IDLE:
                break
            if st == self.ST_ERROR:
                err = self.read_error()
                self.write_ctrl(self.CTRL_CLEAR)
                raise IapError(f"pre-command error status: {err}")
            if time.time() - t0 > poll_timeout:
                raise IapError("timeout waiting IDLE")
            time.sleep(0.001)

        # 2. write mailbox
        self.write_mailbox(frame)

        # 3. commit
        self.write_ctrl(self.CTRL_COMMIT)

        # 4. poll status
        t0 = time.time()
        while True:
            try:
                st = self.read_status()
            except Exception as e:
                # NACK / timeout during Flash stall -> treat as BUSY
                st = self.ST_BUSY
                self.log("inf", f"poll exception (treat as BUSY): {e}")
            if st == self.ST_RESP_READY:
                break
            if st == self.ST_ERROR:
                err = self.read_error()
                self.write_ctrl(self.CTRL_CLEAR)
                raise IapError(f"command error: {err}")
            if time.time() - t0 > poll_timeout:
                self.write_ctrl(self.CTRL_ABORT)
                raise IapError("timeout waiting RESP_READY")
            time.sleep(busy_retry)

        # 5. read tx_len
        tx_len = self.read_tx_len()
        if tx_len < 10 or tx_len > 530:
            self.write_ctrl(self.CTRL_ABORT)
            raise IapError(f"bad tx_len: {tx_len}")

        # 6. read response
        resp_raw = self.read_mailbox(tx_len)

        # 7. parse & validate
        rcmd, rseq, rpayload = self.parse_frame(resp_raw)
        if rcmd != cmd:
            raise IapError(f"cmd mismatch: req={cmd:02X} resp={rcmd:02X}")
        # seq echo is previous self.seq-1; accept either current or previous
        expected_seq = (self.seq - 1) & 0xFF
        if rseq != expected_seq:
            raise IapError(f"seq mismatch: exp={expected_seq:02X} resp={rseq:02X}")

        # 8. clear
        self.write_ctrl(self.CTRL_CLEAR)

        if not rpayload:
            raise IapError("empty response payload")
        err_code = rpayload[0]
        return err_code, rpayload[1:]

    # ------------------------------------------------------------------
    # Individual commands
    # ------------------------------------------------------------------
    def cmd_handshake(self):
        err, resp = self.transaction(self.CMD_HANDSHAKE, b'')
        if err != 0:
            raise IapError(f"HANDSHAKE failed: {err}")
        if len(resp) < 3:
            raise IapError("HANDSHAKE response too short")
        version = resp[0]
        payload_max = int.from_bytes(resp[1:3], 'little')
        return version, payload_max

    def cmd_erase_flash(self, app_size: int):
        err, _ = self.transaction(self.CMD_ERASE_FLASH,
                                  app_size.to_bytes(4, 'little'),
                                  poll_timeout=20.0)
        if err != 0:
            raise IapError(f"ERASE_FLASH failed: {err}")

    def cmd_app_download(self, flash_addr: int, data: bytes):
        payload = (flash_addr & 0xFFFFFFFF).to_bytes(4, 'little') + data
        err, _ = self.transaction(self.CMD_APP_DOWNLOAD, payload,
                                  poll_timeout=5.0)
        if err != 0:
            raise IapError(f"APP_DOWNLOAD @0x{flash_addr:08X} failed: {err}")

    def cmd_crc_flash(self, app_size: int):
        err, resp = self.transaction(self.CMD_CRC_FLASH,
                                      app_size.to_bytes(4, 'little'),
                                      poll_timeout=10.0)
        if err != 0:
            raise IapError(f"CRC_FLASH failed: {err}")
        if len(resp) < 2:
            raise IapError("CRC_FLASH response too short")
        return int.from_bytes(resp[0:2], 'little')

    def cmd_jump_to_app(self):
        err, _ = self.transaction(self.CMD_JUMP_TO_APP, b'', poll_timeout=5.0)
        if err != 0:
            raise IapError(f"JUMP_TO_APP failed: {err}")

    def cmd_jump_to_boot(self):
        """Send JUMP_TO_BOOT to APP — tells APP to reset into Bootloader."""
        err, _ = self.transaction(self.CMD_JUMP_TO_BOOT, b'', poll_timeout=5.0)
        if err != 0:
            raise IapError(f"JUMP_TO_BOOT failed: {err}")

    # ------------------------------------------------------------------
    # Full firmware upgrade flow
    # ------------------------------------------------------------------
    def upgrade_bytes(self, app_bin: bytes, chunk_size=256,
                      progress_callback=None, stop_event=None):
        app_size = len(app_bin)
        if app_size == 0:
            raise IapError("empty app.bin")

        # 1. HANDSHAKE
        version, payload_max = self.cmd_handshake()
        self.log("inf", f"HANDSHAKE OK version={version} payload_max={payload_max}")

        # 2. ERASE_FLASH
        self.log("inf", f"ERASE_FLASH size={app_size}")
        self.cmd_erase_flash(app_size)

        # 3. APP_DOWNLOAD loop
        offset = 0
        while offset < app_size:
            if stop_event and stop_event.is_set():
                self.write_ctrl(self.CTRL_ABORT)
                raise IapError("stopped by user")

            block = app_bin[offset:offset + chunk_size]
            self.cmd_app_download(self.app_addr + offset, block)
            offset += len(block)
            if progress_callback:
                progress_callback(offset, app_size)

        # 4. CRC_FLASH
        local_crc = self.crc16(app_bin)
        self.log("inf", f"local CRC = {local_crc:04X}")
        flash_crc = self.cmd_crc_flash(app_size)
        if flash_crc != local_crc:
            raise IapError(f"CRC mismatch local={local_crc:04X} flash={flash_crc:04X}")
        self.log("inf", f"CRC match {flash_crc:04X}")

        # 5. JUMP_TO_APP
        self.cmd_jump_to_app()
        self.log("inf", "JUMP_TO_APP accepted")

        return True

    def upgrade(self, bin_path: str, chunk_size=256,
                progress_callback=None, stop_event=None):
        with open(bin_path, 'rb') as f:
            app_bin = f.read()
        return self.upgrade_bytes(app_bin, chunk_size, progress_callback, stop_event)
