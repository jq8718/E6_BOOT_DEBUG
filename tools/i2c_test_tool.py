#!/usr/bin/env python3
"""
USB-I2C Bridge GUI Test Tool for ESP32-P4
功能: 单个读写, 批量读写, 总线扫描, 脚本文件批量执行
"""

import sys, os, re, time, json, threading, queue, struct
from datetime import datetime
from pathlib import Path

try:
    import serial
except ImportError:
    print("请安装 pyserial: pip install pyserial")
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    print("需要 tkinter (Python 标准库)")
    sys.exit(1)

from iap import IapProtocol, IapError


class I2CBridge:
    def __init__(self, port, timeout=0.5):
        self.ser = serial.Serial(port, 115200, timeout=timeout)
        self._orig_timeout = timeout
        time.sleep(0.5)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def is_open(self):
        return self.ser and self.ser.is_open

    def send_cmd(self, cmd):
        """Send command, read one line response."""
        self.ser.reset_input_buffer()
        self.ser.write((cmd.strip() + "\r\n").encode("utf-8"))
        self.ser.flush()
        result = self.ser.readline().decode("utf-8", errors="replace").strip()
        return result

    def probe(self, addr):
        r = self.send_cmd(f"I2C_PROBE {addr:02X}")
        return "found" in r.lower()

    def scan(self):
        """Scan bus. I2C_SCAN returns multiline table. Drain all and return."""
        self.ser.write(b"I2C_SCAN\r\n")
        self.ser.flush()
        time.sleep(0.5)
        self.ser.timeout = 0.5
        result = b""
        while True:
            try:
                chunk = self.ser.read(2048)
                if not chunk:
                    break
                result += chunk
            except:
                break
        self.ser.timeout = self._orig_timeout if hasattr(self, '_orig_timeout') else 3.0
        return result.decode("utf-8", errors="replace").strip()

    def write_reg(self, dev, reg, data):
        dh = " ".join(f"{b:02X}" for b in data)
        return self.send_cmd(f"I2C_WR {dev:02X} {reg:02X} {dh}")

    def read_reg(self, dev, reg, length=1):
        resp = self.send_cmd(f"I2C_RD {dev:02X} {reg:02X} {length}")
        m = re.search(r"=\s*([0-9A-Fa-f ]+)$", resp)
        return m.group(1).strip() if m else "?"

    def info(self):
        return self.send_cmd("INFO")


def parse_script(path):
    """
    WR <dev_hex> <reg_hex> <data_hex>...
    RD <dev_hex> <reg_hex> <len_dec>
    SLEEP <ms>
    """
    cmds = []
    with open(path, "r", encoding="utf-8") as f:
        for i, raw in enumerate(f, 1):
            line = raw.split("#")[0].strip()
            if not line:
                continue
            parts = line.split()
            try:
                op = parts[0].upper()
                if op == "WR":
                    dev = int(parts[1], 16)
                    reg = int(parts[2], 16)
                    data = [int(x, 16) for x in parts[3:]]
                    cmds.append(("WR", dev, reg, data))
                elif op == "RD":
                    dev = int(parts[1], 16)
                    reg = int(parts[2], 16)
                    cmds.append(("RD", dev, reg, int(parts[3])))
                elif op == "SLEEP":
                    cmds.append(("SLEEP", int(parts[1]), 0, 0))
                else:
                    raise ValueError(f"Unknown: {op}")
            except Exception as e:
                raise ValueError(f"Line {i}: {line}\n  {e}")
    return cmds


class I2CTestApp:
    W = 1350
    H = 780

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("USB-I2C Bridge 测试工具 v1.0")
        self.root.geometry(f"{self.W}x{self.H}")
        self.root.minsize(1300, 680)

        self.bridge = None
        self.cmd_queue = queue.Queue()
        self.script_running = False
        self.script_commands = []
        self.selected_file = None

        # IAP state
        self.iap_file = None
        self.iap_bin_data = None
        self.iap_running = False
        self.iap_stop_event = None
        self.iap_total_size = 0
        self.iap_payload_max = 0

        self._refresh_ports()
        self._build_ui()
        self._poll()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _refresh_ports(self):
        try:
            import serial.tools.list_ports
            self._port_list = [p.device for p in serial.tools.list_ports.comports()]
        except:
            self._port_list = []

    def _build_ui(self):
        mb = tk.Menu(self.root)
        self.root.config(menu=mb)
        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label="导入脚本...", command=self._on_select_file, accelerator="Ctrl+O")
        fm.add_separator()
        fm.add_command(label="退出", command=self.root.quit, accelerator="Ctrl+Q")
        mb.add_cascade(label="文件", menu=fm)

        hm = tk.Menu(mb, tearoff=0)
        hm.add_command(label="帮助", command=self._show_help)
        mb.add_cascade(label="帮助", menu=hm)

        pw = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        pw.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        left = ttk.Frame(pw)
        right = ttk.Frame(pw)
        pw.add(left, weight=2)
        pw.add(right, weight=3)

        nb = ttk.Notebook(left)
        nb.pack(fill=tk.BOTH, expand=True)
        self._build_conn_tab(nb)
        self._build_single_tab(nb)
        self._build_batch_tab(nb)
        self._build_scan_tab(nb)
        self._build_script_tab(nb)
        self._build_iap_tab(nb)
        self._build_hex_merge_tab(nb)

        ttk.Label(right, text="通信日志", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 4))
        self.log = scrolledtext.ScrolledText(
            right, wrap=tk.WORD, font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        self.log.pack(fill=tk.BOTH, expand=True)

        self.log.tag_config("ts", foreground="#6a9955")
        self.log.tag_config("tx", foreground="#569cd6")
        self.log.tag_config("rx", foreground="#ce9178")
        self.log.tag_config("err", foreground="#f44747")
        self.log.tag_config("inf", foreground="#9cdcfe")

        ttk.Button(right, text="清空", command=self._clear_log).pack(anchor=tk.E, pady=(4, 0))

    def _add_tab(self, nb, title):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text=title)
        return f

    def _build_conn_tab(self, nb):
        f = self._add_tab(nb, "连接")
        self.port_var = tk.StringVar(value=self._port_list[0] if self._port_list else "COM3")

        ttk.Label(f, text="端口:").grid(row=0, column=0, sticky=tk.W, pady=4)
        pf = ttk.Frame(f)
        pf.grid(row=0, column=1, sticky=tk.EW, padx=4)
        self.port_combo = ttk.Combobox(pf, textvariable=self.port_var, width=18, values=self._port_list)
        self.port_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(pf, text=chr(8635), width=3, command=self._refresh_ports).pack(side=tk.RIGHT, padx=(4,0))

        self.btn_conn = ttk.Button(f, text="连接", command=self._on_connect)
        self.btn_conn.grid(row=0, column=2, padx=4)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=8)

        self.status_str = tk.StringVar(value="Disconnected")
        ttk.Label(f, text="状态:").grid(row=2, column=0, sticky=tk.W)
        self.status_label = ttk.Label(f, textvariable=self.status_str, foreground="red")
        self.status_label.grid(row=2, column=1, sticky=tk.W)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=6)

        ttk.Label(f, text="CLK (kHz):").grid(row=4, column=0, sticky=tk.W)
        fr_clk = ttk.Frame(f)
        fr_clk.grid(row=4, column=1, sticky=tk.W, padx=4)
        self.clk_var = tk.StringVar(value="400")
        clk_entry = ttk.Entry(fr_clk, textvariable=self.clk_var, width=10)
        clk_entry.pack(side=tk.LEFT)
        ttk.Button(fr_clk, text="设置", command=self._on_set_clk).pack(side=tk.LEFT, padx=4)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=6)

        ttk.Label(f, text="I2C 地址:").grid(row=6, column=0, sticky=tk.W)
        vh = (f.register(self._vhx), "%P")
        self.conn_dev_addr = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.conn_dev_addr.insert(0, "32")
        self.conn_dev_addr.grid(row=6, column=1, sticky=tk.W, padx=4)
        self.btn_fw_version = ttk.Button(f, text="读固件版本", command=self._on_read_fw_version)
        self.btn_fw_version.grid(row=6, column=2, padx=4)

        self.fw_version_str = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.fw_version_str, foreground="blue").grid(row=7, column=0, columnspan=3, sticky=tk.W, pady=2)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=8, column=0, columnspan=3, sticky=tk.EW, pady=6)
        ttk.Label(f, text="Info available in Log", foreground="gray").grid(row=9, column=0, sticky=tk.W, pady=2)

    def _build_single_tab(self, nb):
        f = self._add_tab(nb, "单个读写")
        vh = (f.register(self._vhx), "%P")

        def er(r, c, txt, **kw):
            ttk.Label(f, text=txt).grid(row=r, column=c, sticky=tk.W, **kw)

        er(0, 0, "Dev Addr (hex):", pady=4)
        self.s_dev = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.s_dev.insert(0, "32")
        self.s_dev.grid(row=0, column=1, sticky=tk.W, padx=4)

        er(0, 2, "Reg (hex):", padx=(16, 0), pady=4)
        self.s_reg = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.s_reg.insert(0, "00")
        self.s_reg.grid(row=0, column=3, sticky=tk.W, padx=4)

        er(1, 0, "Data (hex):", pady=4)
        self.s_data = ttk.Entry(f, width=40)
        self.s_data.insert(0, "FF")
        self.s_data.grid(row=1, column=1, columnspan=3, sticky=tk.EW, padx=4)

        bf = ttk.Frame(f)
        bf.grid(row=2, column=0, columnspan=4, pady=6)
        ttk.Button(bf, text="读取", command=self._on_single_read).pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text="写入", command=self._on_single_write).pack(side=tk.LEFT, padx=4)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=4, sticky=tk.EW, pady=8)

        ttk.Label(f, text="Quick Write:", font=("", 9, "bold")).grid(row=4, column=0, sticky=tk.W, pady=4)
        er(5, 0, "Dev Addr (hex):", pady=2)
        self.q_dev = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.q_dev.insert(0, "32")
        self.q_dev.grid(row=5, column=1, sticky=tk.W, padx=4)
        er(5, 2, "Reg + Data (hex):", padx=(16, 0))
        self.q_data = ttk.Entry(f, width=30)
        self.q_data.grid(row=5, column=3, sticky=tk.EW, padx=4)
        ttk.Button(f, text="执行", command=self._on_quick_write).grid(row=12, column=0, pady=4, sticky=tk.W)

    def _build_batch_tab(self, nb):
        f = self._add_tab(nb, "批量读写")
        vh = (f.register(self._vhx), "%P")

        def er(r, c, txt, **kw):
            ttk.Label(f, text=txt).grid(row=r, column=c, sticky=tk.W, **kw)

        er(0, 0, "设备地址 (hex):", pady=4)
        self.b_dev = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.b_dev.insert(0, "32")
        self.b_dev.grid(row=0, column=1, sticky=tk.W, padx=4)

        er(1, 0, "起始寄存器 (hex):", pady=4)
        self.b_rs = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.b_rs.insert(0, "00")
        self.b_rs.grid(row=1, column=1, sticky=tk.W, padx=4)

        er(1, 2, "结束寄存器 (hex):", padx=(16, 0), pady=4)
        self.b_re = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.b_re.insert(0, "0F")
        self.b_re.grid(row=1, column=3, sticky=tk.W, padx=4)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=6)

        # 写入模式
        ttk.Label(f, text="写入模式:", font=("", 9, "bold")).grid(row=3, column=0, sticky=tk.W, pady=4)
        self.b_mode = tk.StringVar(value="固定值")
        ttk.Radiobutton(f, text="批量值", variable=self.b_mode, value="批量值").grid(row=3, column=1, sticky=tk.W)
        ttk.Radiobutton(f, text="递增值", variable=self.b_mode, value="递增值").grid(row=3, column=2, sticky=tk.W)
        ttk.Radiobutton(f, text="固定值", variable=self.b_mode, value="固定值").grid(row=3, column=3, sticky=tk.W)
        self.b_fill = ttk.Entry(f, width=10, validate="key", validatecommand=vh)
        self.b_fill.insert(0, "00")
        self.b_fill.grid(row=4, column=3, sticky=tk.W, padx=(0, 4))

        ttk.Label(f, text="批量数据 (hex,空格/换行):", foreground="gray").grid(row=5, column=0, columnspan=4, sticky=tk.W, pady=2)
        self.b_data = tk.Text(f, height=14, width=60, font=("Consolas", 9))
        self.b_data.insert("1.0", "00 01 02 03 04 05 06 07\n08 09 0A 0B 0C 0D 0E 0F")
        self.b_data.grid(row=6, column=0, columnspan=4, sticky=tk.EW, padx=4, pady=2)

        bf_btn = ttk.Frame(f)
        bf_btn.grid(row=7, column=0, columnspan=4, pady=6)
        ttk.Button(bf_btn, text="批量读取（连续）", command=self._on_batch_read).pack(side=tk.LEFT, padx=2)
        ttk.Button(bf_btn, text="批量读取（逐个）", command=self._on_batch_read_individual).pack(side=tk.LEFT, padx=2)
        ttk.Button(bf_btn, text="批量写入（连续）", command=self._on_batch_write_continuous).pack(side=tk.LEFT, padx=2)
        ttk.Button(bf_btn, text="批量写入（逐个）", command=self._on_batch_write).pack(side=tk.LEFT, padx=2)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=8, column=0, columnspan=4, sticky=tk.EW, pady=8)
        ttk.Label(f, text="读取结果:", font=("", 9, "bold")).grid(row=9, column=0, sticky=tk.W, pady=4)
        self.b_res = tk.Text(f, height=14, width=60, font=("Consolas", 9))
        self.b_res.grid(row=10, column=0, columnspan=4, sticky=tk.EW, pady=4)

        bf2 = ttk.Frame(f)
        bf2.grid(row=11, column=0, columnspan=4, sticky=tk.W, pady=2)
        ttk.Button(bf2, text="复制结果", command=self._on_copy_batch).pack(side=tk.LEFT, padx=4)
        ttk.Button(bf2, text="清除", command=self._on_clear_batch).pack(side=tk.LEFT, padx=4)

    def _build_scan_tab(self, nb):
        f = self._add_tab(nb, "总线扫描")
        ttk.Label(f, text="扫描 I2C 总线上的设备。").pack(anchor=tk.W, pady=6)
        ttk.Button(f, text="开始扫描", command=self._on_scan).pack(anchor=tk.W, pady=4)
        self.scan_res = tk.Text(f, height=14, width=50, font=("Consolas", 9))
        self.scan_res.pack(fill=tk.BOTH, expand=True, pady=4)

    def _build_script_tab(self, nb):
        f = self._add_tab(nb, "脚本发送")
        help_text = (
            "脚本文件格式:\n"
            "  WR <dev_hex> <reg_hex> <data_hex>...\n"
            "  RD <dev_hex> <reg_hex> <len>\n"
            "  SLEEP <ms>\n"
            "  # comment\n"
            "Eg: WR 18 03 FF AA\n"
            "    SLEEP 10\n"
        )
        ttk.Label(f, text=help_text, justify=tk.LEFT, foreground="gray").pack(anchor=tk.W, pady=4)

        self.file_str = tk.StringVar(value="未选择文件")
        ttk.Label(f, textvariable=self.file_str, foreground="blue").pack(anchor=tk.W, pady=4)

        bf = ttk.Frame(f)
        bf.pack(fill=tk.X, pady=4)
        ttk.Button(bf, text="选择文件...", command=self._on_select_file).pack(side=tk.LEFT, padx=4)
        self.btn_script = ttk.Button(bf, text=chr(9654)+" Run", command=self._on_run_script)
        self.btn_script.pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text=chr(9632)+" Stop", command=self._on_stop_script).pack(side=tk.LEFT, padx=4)

        ttk.Separator(f, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
        ttk.Label(f, text="执行进度:", font=("", 9, "bold")).pack(anchor=tk.W)
        self.pbar = ttk.Progressbar(f, mode="determinate")
        self.pbar.pack(fill=tk.X, pady=4)
        self.progress_str = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.progress_str).pack(anchor=tk.W)

    def _build_iap_tab(self, nb):
        f = self._add_tab(nb, "IAP 升级")
        vh = (f.register(self._vhx), "%P")
        vh32 = (f.register(self._vhx32), "%P")

        def er(r, c, txt, **kw):
            ttk.Label(f, text=txt).grid(row=r, column=c, sticky=tk.W, **kw)

        er(0, 0, "I2C 设备地址 (hex):", pady=4)
        self.iap_dev = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.iap_dev.insert(0, "32")
        self.iap_dev.grid(row=0, column=1, sticky=tk.W, padx=(2, 8))

        er(0, 2, "APP 基地址 (hex):", padx=(8, 0), pady=4)
        self.iap_addr = ttk.Entry(f, width=12, validate="key", validatecommand=vh32)
        self.iap_addr.insert(0, "2000")
        self.iap_addr.grid(row=0, column=3, sticky=tk.W, padx=(2, 8))

        er(1, 0, "每包固件数据字节数:", pady=4)
        self.iap_chunk = ttk.Entry(f, width=12)
        self.iap_chunk.insert(0, "512")
        self.iap_chunk.grid(row=1, column=1, sticky=tk.W, padx=(2, 8))

        er(1, 2, "FLASH 延时(ms):", padx=(8, 0), pady=4)
        self.iap_flash_delay = ttk.Entry(f, width=12)
        self.iap_flash_delay.insert(0, "500")
        self.iap_flash_delay.grid(row=1, column=3, sticky=tk.W, padx=(2, 8))

        # 提交后延时复选框
        self.iap_commit_delay_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="提交后延时", variable=self.iap_commit_delay_var).grid(
            row=1, column=4, sticky=tk.W, padx=(12, 0), pady=4)

        # file selection
        self.iap_file_str = tk.StringVar(value="未选择 app.bin")
        ttk.Label(f, textvariable=self.iap_file_str, foreground="blue").grid(
            row=2, column=0, columnspan=5, sticky=tk.W, pady=4)

        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, columnspan=5, sticky=tk.W, pady=4)
        style = ttk.Style()
        style.configure('IAP.TButton', padding=(1, 0))
        ttk.Button(bf, text="选择文件", style='IAP.TButton', command=self._on_iap_select_file).pack(side=tk.LEFT, padx=1)
        self.btn_iap_boot = ttk.Button(bf, text="进入升级", style='IAP.TButton', command=self._on_iap_enter_boot)
        self.btn_iap_boot.pack(side=tk.LEFT, padx=1)
        self.btn_iap_hs = ttk.Button(bf, text="握手", style='IAP.TButton', command=self._on_iap_handshake)
        self.btn_iap_hs.pack(side=tk.LEFT, padx=1)
        self.btn_iap_erase = ttk.Button(bf, text="擦除", style='IAP.TButton', command=self._on_iap_erase)
        self.btn_iap_erase.pack(side=tk.LEFT, padx=1)
        self.btn_iap_dl = ttk.Button(bf, text="下载", style='IAP.TButton', command=self._on_iap_download)
        self.btn_iap_dl.pack(side=tk.LEFT, padx=1)
        self.btn_iap_crc = ttk.Button(bf, text="校验", style='IAP.TButton', command=self._on_iap_crc)
        self.btn_iap_crc.pack(side=tk.LEFT, padx=1)
        self.btn_iap_jump = ttk.Button(bf, text="跳转", style='IAP.TButton', command=self._on_iap_jump)
        self.btn_iap_jump.pack(side=tk.LEFT, padx=1)
        ttk.Button(bf, text="自动", style='IAP.TButton', command=self._on_iap_auto).pack(side=tk.LEFT, padx=1)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=4, column=0, columnspan=5, sticky=tk.EW, pady=8)

        ttk.Label(f, text="升级进度:", font=("", 9, "bold")).grid(row=5, column=0, sticky=tk.W)
        self.pbar_iap = ttk.Progressbar(f, mode="determinate")
        self.pbar_iap.grid(row=6, column=0, columnspan=5, sticky=tk.EW, pady=4)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=5, sticky=tk.EW, pady=8)

        # bin content display
        ttk.Label(f, text="app.bin 内容 (hex):", font=("", 9, "bold")).grid(row=9, column=0, sticky=tk.W)
        f_bin = ttk.Frame(f)
        f_bin.grid(row=10, column=0, columnspan=5, sticky=tk.NSEW, pady=4)
        self.iap_bin = tk.Text(f_bin, height=6, width=70, font=("Consolas", 9), state=tk.DISABLED)
        self.iap_bin.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_bin = ttk.Scrollbar(f_bin, orient=tk.VERTICAL, command=self.iap_bin.yview)
        sb_bin.pack(side=tk.RIGHT, fill=tk.Y)
        self.iap_bin.configure(yscrollcommand=sb_bin.set)

        f.columnconfigure(5, weight=0)
        f.rowconfigure(10, weight=1)

    def _build_hex_merge_tab(self, nb):
        f = self._add_tab(nb, "HEX合并")
        self.hex_boot_path = tk.StringVar()
        self.hex_app_path = tk.StringVar()
        self.hex_boot_data = None  # (data, start_addr)
        self.hex_app_data = None

        # row 0: boot hex
        ttk.Label(f, text="BOOT.HEX:").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(f, textvariable=self.hex_boot_path, width=50).grid(row=0, column=1, sticky=tk.EW, padx=4)
        ttk.Button(f, text="选择", command=lambda: self._hex_sel("boot")).grid(row=0, column=2, padx=2)

        # row 1: app hex
        ttk.Label(f, text="APP.HEX:").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(f, textvariable=self.hex_app_path, width=50).grid(row=1, column=1, sticky=tk.EW, padx=4)
        ttk.Button(f, text="选择", command=lambda: self._hex_sel("app")).grid(row=1, column=2, padx=2)

        # row 2: params
        pf = ttk.Frame(f)
        pf.grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=6)
        ttk.Label(pf, text="APP版本:").pack(side=tk.LEFT)
        self.hex_app_ver = ttk.Entry(pf, width=6, state="readonly")
        self.hex_app_ver.configure(state="normal")
        self.hex_app_ver.insert(0, "1")
        self.hex_app_ver.configure(state="readonly")
        self.hex_app_ver.pack(side=tk.LEFT, padx=(2, 16))

        ttk.Label(pf, text="APP基址:").pack(side=tk.LEFT)
        self.hex_app_base = ttk.Entry(pf, width=8, state="readonly")
        self.hex_app_base.configure(state="normal")
        self.hex_app_base.insert(0, "2000")
        self.hex_app_base.configure(state="readonly")
        self.hex_app_base.pack(side=tk.LEFT, padx=(2, 16))

        ttk.Label(pf, text="BOOT参数:").pack(side=tk.LEFT)
        self.hex_boot_param = ttk.Entry(pf, width=8, state="readonly")
        self.hex_boot_param.configure(state="normal")
        self.hex_boot_param.insert(0, "1E00")
        self.hex_boot_param.configure(state="readonly")
        self.hex_boot_param.pack(side=tk.LEFT, padx=2)

        self.hex_param_lock = tk.BooleanVar(value=False)
        ttk.Checkbutton(pf, text="允许修改", variable=self.hex_param_lock,
            command=self._hex_toggle_param_lock).pack(side=tk.LEFT, padx=(12, 4))

        ttk.Button(pf, text="计算BOOT参数", command=self._hex_calc_params).pack(side=tk.LEFT, padx=(16, 4))
        ttk.Button(pf, text="合并保存...", command=self._hex_merge).pack(side=tk.LEFT, padx=4)

        # row 3: hex viewer (address + data display)
        vf = ttk.Frame(f)
        vf.grid(row=3, column=0, columnspan=4, sticky=tk.NSEW, pady=4)
        self.hex_viewer = scrolledtext.ScrolledText(vf, wrap=tk.NONE, font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", state=tk.DISABLED)
        self.hex_viewer.pack(fill=tk.BOTH, expand=True)
        self.hex_viewer.tag_config("addr", foreground="#6a9955")
        self.hex_viewer.tag_config("data", foreground="#d4d4d4")
        self.hex_viewer.tag_config("label", foreground="#569cd6")
        self.hex_viewer.tag_config("info", foreground="#ce9178")

        # row 4: log
        ttk.Label(f, text="运行日志:", font=("", 9, "bold")).grid(row=4, column=0, sticky=tk.W, pady=(4, 0))
        self.hex_log = scrolledtext.ScrolledText(f, wrap=tk.WORD, font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", height=6, state=tk.DISABLED)
        self.hex_log.tag_config("ok", foreground="#4ec9b0")
        self.hex_log.tag_config("warn", foreground="#ce9178")
        self.hex_log.tag_config("err", foreground="#f44747")
        self.hex_log.grid(row=5, column=0, columnspan=4, sticky=tk.NSEW, pady=4)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(3, weight=1)

    # ----- Handlers -----

    def _on_set_clk(self):
        if not self._ck(): return
        try:
            freq_khz = int(self.clk_var.get().strip())
            if freq_khz < 10 or freq_khz > 400:
                raise ValueError("范围 10~400")
            freq_hz = freq_khz * 1000
        except ValueError as e:
            messagebox.showerror("Error", f"无效频率: {e}")
            return
        self.log_add(f"I2C_FREQ {freq_hz}", "tx")
        try:
            r = self.bridge.send_cmd(f"I2C_FREQ {freq_hz}")
            self.log_add(f"= {r}", "rx")
        except Exception as e:
            self.log_add(f"ERR: {e}", "err")

    def _on_read_fw_version(self):
        if not self._ck(): return
        try:
            dev = int(self.conn_dev_addr.get().strip(), 16)
        except ValueError:
            messagebox.showerror("Error", "设备地址必须是十六进制")
            return
        self.log_add(f"读取固件版本: dev=0x{dev:02X}", "tx")
        try:
            # REG_FW_VERSION_LOW  = 0x04
            # REG_FW_VERSION_HIGH = 0x05
            r_low = self.bridge.send_cmd(f"I2C_RD {dev:02X} 04 1")
            r_high = self.bridge.send_cmd(f"I2C_RD {dev:02X} 05 1")
            # Parse response: "OK: Read 0xXX reg 0xXX = HH"
            low = int(r_low.strip().split()[-1], 16)
            high = int(r_high.strip().split()[-1], 16)
            ver = (high << 8) | low
            kind = "Boot" if (ver & 0x8000) else "APP"
            self.fw_version_str.set(f"{kind} 版本 0x{ver & 0x7FFF:04X}")
            self.log_add(f"固件版本: {kind} 0x{ver & 0x7FFF:04X} (raw=0x{ver:04X})", "rx")
        except Exception as e:
            self.log_add(f"读取固件版本失败: {e}", "err")
            self.fw_version_str.set("")

    def _hex_sel(self, which):
        path = filedialog.askopenfilename(
            title="选择 " + ("BOOT" if which == "boot" else "APP") + ".HEX",
            filetypes=[("HEX files", "*.hex"), ("All files", "*.*")])
        if not path:
            return
        if which == "boot":
            self.hex_boot_path.set(path)
        else:
            self.hex_app_path.set(path)
        self._hex_load(which)

    def _hex_toggle_param_lock(self):
        st = tk.NORMAL if self.hex_param_lock.get() else "readonly"
        self.hex_app_ver.configure(state=tk.NORMAL)
        self.hex_app_ver.configure(state=st)
        self.hex_app_base.configure(state=tk.NORMAL)
        self.hex_app_base.configure(state=st)
        self.hex_boot_param.configure(state=tk.NORMAL)
        self.hex_boot_param.configure(state=st)

    def _hex_display(self, data, start, tag="boot"):
        tv = self.hex_viewer
        tv.configure(state=tk.NORMAL)
        label = "BOOT" if tag == "boot" else "APP"
        tv.insert(tk.END, f"── {label} @ 0x{start:04X}, {len(data)} bytes ──\n", "label")
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]; addr = start + i
            hex_str = " ".join(f"{b:02X}" for b in chunk)
            ascii_str = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
            tv.insert(tk.END, f"{addr:04X}  ", "addr")
            tv.insert(tk.END, f"{hex_str:<48}  {ascii_str}\n", "data")
        tv.configure(state=tk.DISABLED)

    def _hex_load(self, which):
        path = self.hex_boot_path.get() if which == "boot" else self.hex_app_path.get()
        if not path:
            return
        try:
            data, start = self._hex_parse(path)
            size = len(data)
            end = start + size

            if which == "boot":
                # BOOT: must start at 0x0000, must not exceed parameter area (0x1E00)
                if start != 0x0000:
                    raise ValueError(f"BOOT 起始地址应为 0x0000，实际为 0x{start:04X}")
                if end > 0x1E00:
                    raise ValueError(f"BOOT 越界: 结束于 0x{end:04X}，不能超过 0x1E00 (参数区起始)")
                self.hex_boot_data = (data, start)
                self._hex_log(f"BOOT: {size} bytes @ 0x{start:04X} ~ 0x{end-1:04X}")
            else:
                try:
                    app_base = int(self.hex_app_base.get().strip(), 16)
                except ValueError:
                    app_base = 0x2000
                if start != app_base:
                    raise ValueError(f"APP 起始地址应为 0x{app_base:04X}，实际为 0x{start:04X}")
                if end > 0xFE00:
                    raise ValueError(f"APP 越界: 结束于 0x{end:04X}，不能超过 0xFE00 (保留区起始)")
                self.hex_app_data = (data, start)
                self._hex_log(f"APP: {size} bytes @ 0x{start:04X} ~ 0x{end-1:04X}")

            # Refresh viewer
            self.hex_viewer.configure(state=tk.NORMAL)
            self.hex_viewer.delete("1.0", tk.END)
            if self.hex_boot_data:
                self._hex_display(self.hex_boot_data[0], self.hex_boot_data[1], "boot")
            if self.hex_app_data:
                tv = self.hex_viewer
                tv.insert(tk.END, "\n", "data")
                self._hex_display(self.hex_app_data[0], self.hex_app_data[1], "app")
        except Exception as e:
            self._hex_log(f"载入失败: {e}")
            messagebox.showerror("载入失败", str(e))

    @staticmethod
    def _crc16(data: bytes) -> int:
        """Same CRC16 as HC32_CalCrc16: init=0xA28C, poly=0x8408, output inverted."""
        crc = 0xA28C
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0x8408
                else:
                    crc >>= 1
        return (~crc) & 0xFFFF

    def _hex_log(self, msg, tag=None):
        self.hex_log.configure(state=tk.NORMAL)
        self.hex_log.insert(tk.END, msg + "\n", tag)
        self.hex_log.see(tk.END)
        self.hex_log.configure(state=tk.DISABLED)

    def _hex_parse(self, path):
        """Parse Intel HEX file into {addr: bytes} and return sorted (addr, data) list."""
        records = {}
        base = 0
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # :LLAAAATT[DD...]CC
                rec_len = int(line[1:3], 16)
                addr = int(line[3:7], 16)
                rtype = int(line[7:9], 16)
                data_str = line[9:9+rec_len*2]
                if rtype == 0x00:
                    full_addr = base + addr
                    for i in range(rec_len):
                        b = int(data_str[i*2:i*2+2], 16)
                        records[full_addr + i] = b
                elif rtype == 0x02:
                    base = int(data_str, 16) << 4
                elif rtype == 0x04:
                    base = int(data_str, 16) << 16
        if not records:
            return []
        addrs = sorted(records.keys())
        start = addrs[0]
        data = []
        for a in addrs:
            data.append(records[a])
        return bytes(data), start

    def _hex_calc_params(self):
        """Calculate and display BOOT parameter table in viewer."""
        if not self.hex_app_data:
            messagebox.showwarning("警告", "请先载入 APP.HEX")
            return
        try:
            app_ver = int(self.hex_app_ver.get().strip())
        except ValueError:
            messagebox.showerror("错误", "请输入有效 APP 版本号")
            return
        try:
            param_base = int(self.hex_boot_param.get().strip(), 16)
        except ValueError:
            messagebox.showerror("错误", "请输入有效 BOOT 参数地址")
            return

        app_bin, app_start = self.hex_app_data

        struct = __import__('struct')
        MAGIC = 0x48434C42
        STATE_IMAGE_VALID = 0x55AAAA55
        APP_ADDR = 0x2000

        app_crc = self._crc16(app_bin)
        app_size = len(app_bin)

        header = struct.pack('<IIIIIII',
            MAGIC, STATE_IMAGE_VALID, APP_ADDR,
            app_size, app_crc, 0, app_ver)
        header_crc = self._crc16(header)
        param_table = header + struct.pack('<I', header_crc)

        # Display in viewer
        tv = self.hex_viewer
        tv.configure(state=tk.NORMAL)
        tv.insert(tk.END, f"\n── BOOT参数表 @ 0x{param_base:04X}, 32 bytes ──\n", "label")
        for i in range(0, len(param_table), 16):
            chunk = param_table[i:i + 16]
            addr = param_base + i
            hex_str = " ".join(f"{b:02X}" for b in chunk)
            tv.insert(tk.END, f"{addr:04X}  ", "addr")
            tv.insert(tk.END, f"{hex_str:<48}\n", "data")

        tv.insert(tk.END, "\n", "data")
        tv.insert(tk.END, f"magic       = 0x{MAGIC:08X}  (\"HCLB\")\n", "info")
        tv.insert(tk.END, f"state       = 0x{STATE_IMAGE_VALID:08X}  (IMAGE_VALID)\n", "info")
        tv.insert(tk.END, f"app_addr    = 0x{APP_ADDR:08X}\n", "info")
        tv.insert(tk.END, f"app_size    = {app_size} (0x{app_size:X})\n", "info")
        tv.insert(tk.END, f"app_crc     = 0x{app_crc:04X}\n", "info")
        tv.insert(tk.END, f"boot_count  = 0\n", "info")
        tv.insert(tk.END, f"app_version = {app_ver}\n", "info")
        tv.insert(tk.END, f"header_crc  = 0x{header_crc:04X}\n", "info")
        tv.configure(state=tk.DISABLED)
        self._hex_log("BOOT 参数表计算完成")

    def _hex_merge(self):
        if not self.hex_boot_data or not self.hex_app_data:
            messagebox.showwarning("警告", "请先选择并载入 BOOT.HEX 和 APP.HEX")
            return
        try:
            app_ver = int(self.hex_app_ver.get().strip())
        except ValueError:
            self._hex_log("错误: APP 版本号无效", "err")
            return
        try:
            app_base = int(self.hex_app_base.get().strip(), 16)
        except ValueError:
            self._hex_log("错误: APP 基址无效", "err")
            return
        try:
            param_base = int(self.hex_boot_param.get().strip(), 16)
        except ValueError:
            self._hex_log("错误: BOOT 参数地址无效", "err")
            return

        self.hex_log.configure(state=tk.NORMAL)
        self.hex_log.delete("1.0", tk.END)
        self.hex_log.configure(state=tk.DISABLED)

        boot_bin, boot_start = self.hex_boot_data
        app_bin, app_start = self.hex_app_data

        # 1. check BOOT
        if boot_start != 0x0000:
            self._hex_log("✖ BOOT 程序区起始地址不是 0x0000", "err")
            return
        boot_end = boot_start + len(boot_bin)
        if boot_end > param_base:
            self._hex_log(f"✖ BOOT 程序区越界 (结束 0x{boot_end:04X}，参数区 0x{param_base:04X})", "err")
            return
        self._hex_log(f"✓ BOOT 程序区: {len(boot_bin)} bytes @ 0x{boot_start:04X}~0x{boot_end-1:04X}", "ok")

        # 2. check APP
        if app_start != app_base:
            self._hex_log(f"✖ APP 起始地址应为 0x{app_base:04X}，实际 0x{app_start:04X}", "err")
            return
        app_end = app_start + len(app_bin)
        if app_end > 0xFE00:
            self._hex_log(f"✖ APP 越界 (结束 0x{app_end:04X}，最大 0xFE00)", "err")
            return
        self._hex_log(f"✓ APP 程序区: {len(app_bin)} bytes @ 0x{app_start:04X}~0x{app_end-1:04X}", "ok")

        # 3. check param area
        if param_base + 32 > 0x2000:
            self._hex_log("✖ BOOT 参数地址越界", "err")
            return
        self._hex_log(f"✓ BOOT 参数区: 32 bytes @ 0x{param_base:04X}", "ok")

        # Build parameter table
        MAGIC = 0x48434C42
        STATE_IMAGE_VALID = 0x55AAAA55
        APP_ADDR = 0x2000
        app_crc = self._crc16(app_bin)
        app_size = len(app_bin)

        header = struct.pack('<IIIIIII',
            MAGIC, STATE_IMAGE_VALID, APP_ADDR,
            app_size, app_crc, 0, app_ver)
        header_crc = self._crc16(header)
        param_table = header + struct.pack('<I', header_crc)

        self._hex_log(f"✓ 参数表: magic=0x{MAGIC:08X} state=0x{STATE_IMAGE_VALID:08X}", "info")
        self._hex_log(f"  app_addr=0x{APP_ADDR:04X} app_size={app_size}(0x{app_size:X})", "info")
        self._hex_log(f"  app_crc=0x{app_crc:04X} app_version={app_ver} header_crc=0x{header_crc:04X}", "info")

        # Merge
        FLASH_SIZE = 0x10000
        merged = bytearray([0xFF] * FLASH_SIZE)
        for i, b in enumerate(boot_bin):
            merged[boot_start + i] = b
        for i, b in enumerate(app_bin):
            merged[app_start + i] = b
        for i, b in enumerate(param_table):
            merged[param_base + i] = b
        while len(merged) > 0 and merged[-1] == 0xFF:
            merged.pop()

        self._hex_log(f"✓ 合并完成: {len(merged)} bytes", "ok")

        save_path = filedialog.asksaveasfilename(
            title="保存合并 HEX",
            defaultextension=".hex",
            filetypes=[("HEX files", "*.hex"), ("All files", "*.*")])
        if not save_path:
            self._hex_log("用户取消保存")
            return

        entry = struct.unpack('<I', merged[4:8])[0] if len(merged) > 8 else 0x0000
        self._hex_write_intel_hex(bytes(merged), save_path, entry=entry)
        self._hex_log(f"✓ 已保存: {save_path}", "ok")

    def _hex_write_intel_hex(self, data: bytes, path: str, bytes_per_line=16, entry=None):
        """Write binary data as Intel HEX file."""
        lines = []
        # Extended linear address 0x0000
        lines.append(":020000040000FA")
        addr = 0
        while addr < len(data):
            chunk = data[addr:addr + bytes_per_line]
            rec_len = len(chunk)
            checksum = rec_len
            checksum += (addr >> 8) & 0xFF
            checksum += addr & 0xFF
            checksum += 0x00
            for b in chunk:
                checksum += b
            checksum = (~checksum + 1) & 0xFF
            hex_str = f":{rec_len:02X}{addr:04X}00{chunk.hex().upper()}{checksum:02X}"
            lines.append(hex_str)
            addr += bytes_per_line
        # Entry point (before EOF)
        if entry is not None:
            e = struct.pack('>I', entry)
            cs = 4 + 0 + 0 + 5 + e[0] + e[1] + e[2] + e[3]
            cs = (~cs + 1) & 0xFF
            lines.append(f":04000005{e[0]:02X}{e[1]:02X}{e[2]:02X}{e[3]:02X}{cs:02X}")
        lines.append(":00000001FF")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def _on_connect(self):
        port = self.port_var.get().strip()
        if not port:
            return
        if self.bridge and self.bridge.is_open():
            self.bridge.close()
            self.bridge = None
            self.btn_conn.configure(text="连接")
            self.status_str.set("Disconnected")
            self.status_label.configure(foreground="red")
            self.log_add(f"Disconnected {port}", "inf")
            return
        try:
            self.bridge = I2CBridge(port)
            self.status_str.set(f"Connected: {port}")
            self.status_label.configure(foreground="green")
            self.btn_conn.configure(text="断开")
            self.log_add(f"Connected to {port}", "inf")
            r = self.bridge.info()
            for l in r.split("\r\n"):
                if l.strip():
                    self.log_add(l, "inf")
        except Exception as e:
            self.log_add(f"Connect failed: {e}", "err")
            messagebox.showerror("Error", str(e))

    def _on_single_read(self):
        if not self._ck(): return
        try:
            dev = int(self.s_dev.get().strip(), 16)
            reg = int(self.s_reg.get().strip(), 16)
        except ValueError:
            messagebox.showerror("Error", "Addr/Reg must be hex")
            return
        self.log_add(f"RD 0x{dev:02X} 0x{reg:02X}", "tx")
        try:
            r = self.bridge.read_reg(dev, reg, 1)
            self.log_add(f"= {r}", "rx")
        except Exception as e:
            self.log_add(f"ERR: {e}", "err")

    def _on_single_write(self):
        if not self._ck(): return
        try:
            dev = int(self.s_dev.get().strip(), 16)
            reg = int(self.s_reg.get().strip(), 16)
            ds = self.s_data.get().strip().replace(",", " ").split()
            data = [int(x, 16) for x in ds]
            if not data:
                raise ValueError("empty")
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        dh = " ".join(f"{b:02X}" for b in data)
        self.log_add(f"WR 0x{dev:02X} 0x{reg:02X} {dh}", "tx")
        try:
            r = self.bridge.write_reg(dev, reg, data)
            self.log_add(f"= {r}", "rx")
        except Exception as e:
            self.log_add(f"ERR: {e}", "err")

    def _on_quick_write(self):
        if not self._ck(): return
        try:
            dev = int(self.q_dev.get().strip(), 16)
            parts = self.q_data.get().strip().split()
            if len(parts) < 2:
                raise ValueError("need reg + data")
            reg = int(parts[0], 16)
            data = [int(x, 16) for x in parts[1:]]
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        dh = " ".join(f"{b:02X}" for b in data)
        self.log_add(f"WR 0x{dev:02X} 0x{reg:02X} {dh}", "tx")
        try:
            r = self.bridge.write_reg(dev, reg, data)
            self.log_add(f"= {r}", "rx")
        except Exception as e:
            self.log_add(f"ERR: {e}", "err")

    def _on_scan(self):
        if not self._ck(): return
        self.scan_res.delete(1.0, tk.END)
        self.scan_res.update_idletasks()
        self.log_add("SCAN...", "tx")
        try:
            r = self.bridge.scan()
            self.scan_res.insert(1.0, r)
            self.log_add("Scan done", "rx")
        except Exception as e:
            self.log_add(f"ERR: {e}", "err")

    def _on_batch_read(self):
        """连续读取: 一次性读所有寄存器（快速）."""
        self._do_batch_read(continuous=True)

    def _on_batch_read_individual(self):
        """逐个读取: 每个寄存器单独发命令（慢但可靠）."""
        self._do_batch_read(continuous=False)

    def _do_batch_read(self, continuous=True):
        if not self._ck(): return
        try:
            dev = int(self.b_dev.get().strip(), 16)
            st = int(self.b_rs.get().strip(), 16)
            en = int(self.b_re.get().strip(), 16)
            if st > en or (en - st) > 255:
                raise ValueError("invalid range")
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return

        mode = "连续" if continuous else "逐个"
        self.log_add(f"Batch RD({mode}) 0x{dev:02X} [0x{st:02X}..0x{en:02X}]", "tx")
        count = en - st + 1

        if continuous:
            # 一次多字节读取
            r = self.bridge.send_cmd(f"I2C_RD {dev:02X} {st:02X} {count}")
            vals = []
            m = re.search(r"=\s*([0-9A-Fa-f ]+)$", r)
            if m:
                vals = m.group(1).strip().split()
            lines = []
            for i in range(count):
                lines.append(f"0x{st+i:02X}: {vals[i] if i < len(vals) else '?'}")
        else:
            # 逐个读取
            lines = []
            for addr in range(st, en + 1):
                try:
                    val = self.bridge.read_reg(dev, addr, 1)
                    lines.append(f"0x{addr:02X}: {val}")
                except Exception as e:
                    lines.append(f"0x{addr:02X}: ERR")
                    self.log_add(f"  0x{addr:02X} ERR: {e}", "err")

        self.b_res.delete(1.0, tk.END)
        self.b_res.insert(1.0, "\n".join(lines))
        self.log_add(f"Done: {count} regs", "rx")

    def _on_batch_write(self):
        """逐个写入（原来逻辑）"""
        self._do_batch_write(continuous=False)

    def _on_batch_write_continuous(self):
        """连续写入"""
        self._do_batch_write(continuous=True)

    def _do_batch_write(self, continuous=True):
        """批量写入: 连续=一条命令写连续寄存器, 逐个=每个寄存器单独发命令"""
        if not self._ck(): return
        try:
            dev = int(self.b_dev.get().strip(), 16)
            st = int(self.b_rs.get().strip(), 16)
            en = int(self.b_re.get().strip(), 16)
            if st > en or (en - st) > 255:
                raise ValueError("范围无效")
        except ValueError as e:
            messagebox.showerror("错误", str(e))
            return

        mode = self.b_mode.get()
        tag = "连续" if continuous else "逐个"
        self.log_add(f"批量写入{tag}({mode}) 0x{dev:02X} [0x{st:02X}..0x{en:02X}]", "tx")
        count = en - st + 1

        # 准备数据
        try:
            if mode == "固定值":
                fv = int(self.b_fill.get().strip(), 16) & 0xFF
                data = [fv] * count
            elif mode == "递增值":
                data = [addr & 0xFF for addr in range(st, en + 1)]
            elif mode == "批量值":
                ds = self.b_data.get("1.0", tk.END).strip().split()
                data = [int(x, 16) for x in ds]
                if len(data) < count:
                    self.log_add(f"数据不足: 需{count}个, 有{len(data)}个", "err")
                    count = len(data)
            else:
                messagebox.showerror("错误", "未知模式")
                return
        except Exception as e:
            messagebox.showerror("错误", str(e))
            return

        ok = 0
        if continuous:
            # 连续写入: 一次命令写所有数据到起始地址，数据间隔50us
            # 通过固件I2C_WR完成(单次START+addr+reg+data0+data1+...+STOP)
            if count > 0:
                dh = " ".join(f"{b:02X}" for b in data[:count])
                r = self.bridge.send_cmd(f"I2C_WR {dev:02X} {st:02X} {dh}")
                if "OK" in r or "ok" in r.lower():
                    ok = count
                else:
                    self.log_add(f"  写入失败: {r}", "err")
        else:
            # 逐个写入
            for i in range(count):
                try:
                    r = self.bridge.write_reg(dev, st + i, [data[i]])
                    if "OK" in r or "ok" in r.lower():
                        ok += 1
                    else:
                        self.log_add(f"  0x{st+i:02X} ERR: {r}", "err")
                except Exception as e:
                    self.log_add(f"  0x{st+i:02X} ERR: {e}", "err")

        self.log_add(f"完成: {ok}/{count}", "rx")

    def _on_clear_batch(self):
        self.b_res.delete("1.0", tk.END)

    def _on_select_file(self):
        path = filedialog.askopenfilename(
            title="Select I2C script",
            filetypes=[("Script", "*.txt *.i2c *.csv"), ("All", "*.*")])
        if path:
            self.selected_file = path
            self.file_str.set(path)

    def _on_run_script(self):
        if not self.selected_file:
            messagebox.showwarning("Warning", "Select a file first")
            return
        try:
            cmds = parse_script(self.selected_file)
        except ValueError as e:
            messagebox.showerror("Parse error", str(e))
            return
        if not cmds:
            messagebox.showwarning("Warning", "No commands in file")
            return

        self.script_commands = cmds
        self.log_add(f"Running: {Path(self.selected_file).name} ({len(cmds)} cmds)", "inf")
        self.pbar["maximum"] = len(cmds)
        self.pbar["value"] = 0
        self.progress_str.set(f"0/{len(cmds)}")
        self.script_running = True
        self.btn_script.config(state=tk.DISABLED)
        t = threading.Thread(target=self._script_worker, daemon=True)
        t.start()

    def _script_worker(self):
        for i, cmd in enumerate(self.script_commands):
            if not self.script_running:
                break
            try:
                op = cmd[0]
                if op == "WR":
                    _, dev, reg, data = cmd
                    dh = " ".join(f"{b:02X}" for b in data)
                    self.cmd_queue.put(("log", f"  [{i+1}] WR 0x{dev:02X} 0x{reg:02X} {dh}", "tx"))
                    r = self.bridge.write_reg(dev, reg, data)
                    self.cmd_queue.put(("log", f"  = {r}", "rx"))
                elif op == "RD":
                    _, dev, reg, length = cmd
                    self.cmd_queue.put(("log", f"  [{i+1}] RD 0x{dev:02X} 0x{reg:02X} len={length}", "tx"))
                    r = self.bridge.read_reg(dev, reg, length)
                    self.cmd_queue.put(("log", f"  = {r}", "rx"))
                elif op == "SLEEP":
                    ms = cmd[1]
                    self.cmd_queue.put(("log", f"  [{i+1}] Sleep {ms}ms", "inf"))
                    time.sleep(ms / 1000.0)
            except Exception as e:
                self.cmd_queue.put(("log", f"  [{i+1}] ERR: {e}", "err"))
            self.cmd_queue.put(("prog", i + 1))
        self.cmd_queue.put(("log", "Script done", "inf"))
        self.cmd_queue.put(("done", None))

    def _on_stop_script(self):
        self.script_running = False
        self.log_add("Stopped", "err")

    # ----- IAP helpers -----

    def _iap_get_params(self):
        dev_str = self.iap_dev.get().strip()
        addr_str = self.iap_addr.get().strip()
        if not dev_str:
            raise ValueError("I2C 设备地址不能为空")
        if not addr_str:
            raise ValueError("APP 基地址不能为空")
        dev = int(dev_str, 16)
        app_addr = int(addr_str, 16)
        return dev, app_addr

    def _iap_create(self):
        dev, app_addr = self._iap_get_params()
        fd_ms = int(self.iap_flash_delay.get().strip())
        fd_sec = max(0.0, fd_ms / 1000.0)
        return IapProtocol(self.bridge, dev, app_addr,
                           log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                           flash_delay=fd_sec,
                           commit_delay_enabled=self.iap_commit_delay_var.get())

    def _iap_set_buttons(self, enabled: bool):
        state = tk.NORMAL if enabled else tk.DISABLED
        for b in (self.btn_iap_hs, self.btn_iap_erase, self.btn_iap_dl,
                  self.btn_iap_crc, self.btn_iap_jump,
                  self.btn_iap_boot):
            b.config(state=state)

    def _iap_get_chunk(self):
        chunk = int(self.iap_chunk.get().strip())
        if chunk < 1 or chunk > 512:
            raise ValueError("每包固件数据字节数必须在 1~512")
        return chunk

    def _iap_reset_progress(self):
        self.pbar_iap["value"] = 0

    def _iap_flash_delay_sec(self):
        try:
            return max(0.0, int(self.iap_flash_delay.get().strip()) / 1000.0)
        except:
            return 0.5

    def _iap_log_res(self, msg):
        self.log_add(msg, "inf")

    # ----- IAP handlers -----

    def _on_iap_select_file(self):
        path = filedialog.askopenfilename(
            title="选择 app.bin",
            filetypes=[("BIN files", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        self.iap_file = path
        self.iap_file_str.set(path)
        # Clear previous bin data before loading new file
        self.iap_bin_data = None
        self._iap_display_bin()
        try:
            with open(path, 'rb') as f:
                self.iap_bin_data = f.read()
        except Exception as e:
            messagebox.showerror("Error", f"读取文件失败: {e}")
            return
        # Validate size: flash=64KB (0x0000~0xFFFF), boot=8KB (0x0000~0x1FFF),
        # last 512B sector reserved for boot params → app usable = 0x2000~(0x10000-512)
        FLASH_END = 0x10000 - 512  # 0xFE00
        app_size = len(self.iap_bin_data)
        try:
            app_base = int(self.iap_addr.get().strip(), 16)
        except ValueError:
            app_base = 0x2000
        if app_base + app_size > FLASH_END:
            messagebox.showwarning("警告",
                f"文件 {app_size} 字节超出可用 Flash 范围\n"
                f"APP基地址=0x{app_base:04X}, 结束地址=0x{app_base+app_size:04X}\n"
                f"Flash 64KB, Boot 8KB (0x0000~0x1FFF)\n"
                f"最后一扇区(512B)存参数, 可用: 0x{app_base:04X}~0x{FLASH_END:04X}\n"
                f"当前配置最多 {FLASH_END-app_base} 字节")
            self.iap_bin_data = None
            return
        self._iap_display_bin()
        self._iap_log_res(f"已加载 {path}, 大小 {len(self.iap_bin_data)} bytes")

    def _iap_display_bin(self):
        self.iap_bin.configure(state=tk.NORMAL)
        self.iap_bin.delete("1.0", tk.END)
        if not self.iap_bin_data:
            self.iap_bin.configure(state=tk.DISABLED)
            return
        lines = []
        for i in range(0, len(self.iap_bin_data), 16):
            chunk = self.iap_bin_data[i:i + 16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            lines.append(f"{i:08X}: {hex_part:<48} {ascii_part}")
        self.iap_bin.insert("1.0", "\n".join(lines))
        self.iap_bin.configure(state=tk.DISABLED)

    def _on_iap_enter_boot(self):
        """Send JUMP_TO_BOOT → APP resets into Bootloader → auto-handshake."""
        if not self._ck(): return
        try:
            dev, app_addr = self._iap_get_params()
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_enter_boot,
                             args=(dev, app_addr), daemon=True)
        t.start()

    def _iap_worker_enter_boot(self, dev, app_addr):
        try:
            fd = self._iap_flash_delay_sec()
            cd_enabled = self.iap_commit_delay_var.get()
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                              flash_delay=fd,
                              commit_delay_enabled=cd_enabled)
            iap.cmd_jump_to_boot()
            self.cmd_queue.put(("log", "APP 已收到 JUMP_TO_BOOT，等待 MCU 复位进入 Bootloader...", "inf"))
            # MCU resetting — wait for bootloader to come up
            time.sleep(1.5)
            # auto-handshake
            iap2 = IapProtocol(self.bridge, dev, app_addr,
                               log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                               flash_delay=fd,
                               commit_delay_enabled=cd_enabled)
            version, payload_max = iap2.cmd_handshake()
            self.iap_payload_max = payload_max
            self.cmd_queue.put(("iap_done", True,
                f"进入 Bootloader 成功: version={version}, IAP_FLASH_DATA_MAX={payload_max}", "hs"))
        except Exception as e:
            self.cmd_queue.put(("iap_done", False, f"进入升级失败: {e}", "hs"))

    def _on_iap_handshake(self):
        if not self._ck(): return
        try:
            dev, app_addr = self._iap_get_params()
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        # memo removed
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_handshake,
                             args=(dev, app_addr), daemon=True)
        t.start()

    def _iap_worker_handshake(self, dev, app_addr):
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                              flash_delay=self._iap_flash_delay_sec(),
                              commit_delay_enabled=self.iap_commit_delay_var.get())
            version, payload_max = iap.cmd_handshake()
            self.iap_payload_max = payload_max
            self.cmd_queue.put(("iap_done", True,
                f"握手成功: version={version}, IAP_FLASH_DATA_MAX={payload_max}", "hs"))
        except Exception as e:
            self.cmd_queue.put(("iap_done", False, f"HANDSHAKE failed: {e}", "hs"))

    def _on_iap_erase(self):
        if not self._ck(): return
        if not self.iap_bin_data:
            messagebox.showwarning("Warning", "请先选择 app.bin")
            return
        try:
            dev, app_addr = self._iap_get_params()
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        # memo removed
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_erase,
                             args=(dev, app_addr, len(self.iap_bin_data)), daemon=True)
        t.start()

    def _iap_worker_erase(self, dev, app_addr, app_size):
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                              flash_delay=self._iap_flash_delay_sec(),
                              commit_delay_enabled=self.iap_commit_delay_var.get())
            iap.cmd_erase_flash(app_size)
            self.cmd_queue.put(("iap_done", True,
                f"擦除成功: size={app_size}"))
        except Exception as e:
            self.cmd_queue.put(("iap_done", False, f"擦除失败: {e}"))

    def _on_iap_download(self):
        if not self._ck(): return
        if not self.iap_bin_data:
            messagebox.showwarning("Warning", "请先选择 app.bin")
            return
        try:
            dev, app_addr = self._iap_get_params()
            chunk = self._iap_get_chunk()
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        # memo removed
        self._iap_set_buttons(False)
        self.iap_stop_event = threading.Event()
        self.pbar_iap["maximum"] = len(self.iap_bin_data)
        self.pbar_iap["value"] = 0
        t = threading.Thread(target=self._iap_worker_download,
                             args=(dev, app_addr, chunk), daemon=True)
        t.start()

    def _iap_worker_download(self, dev, app_addr, chunk):
        def progress(done, total):
            self.cmd_queue.put(("iap_prog", done, total))
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                              flash_delay=self._iap_flash_delay_sec(),
                              commit_delay_enabled=self.iap_commit_delay_var.get())
            app_size = len(self.iap_bin_data)
            offset = 0
            while offset < app_size:
                if self.iap_stop_event.is_set():
                    raise IapError("stopped by user")
                block = self.iap_bin_data[offset:offset + chunk]
                iap.cmd_app_download(app_addr + offset, block)
                offset += len(block)
                progress(offset, app_size)
            self.cmd_queue.put(("iap_done", True,
                f"下载成功: {app_size} bytes"))
        except Exception as e:
            self.cmd_queue.put(("iap_done", False, f"下载失败: {e}"))

    def _on_iap_crc(self):
        if not self._ck(): return
        if not self.iap_bin_data:
            messagebox.showwarning("Warning", "请先选择 app.bin")
            return
        try:
            dev, app_addr = self._iap_get_params()
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        # memo removed
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_crc,
                             args=(dev, app_addr, len(self.iap_bin_data)), daemon=True)
        t.start()

    def _iap_worker_crc(self, dev, app_addr, app_size):
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                              flash_delay=self._iap_flash_delay_sec(),
                              commit_delay_enabled=self.iap_commit_delay_var.get())
            local_crc = iap.crc16(self.iap_bin_data)
            flash_crc = iap.cmd_crc_flash(app_size)
            if flash_crc != local_crc:
                raise IapError(
                    f"CRC mismatch local={local_crc:04X} flash={flash_crc:04X}")
            self.cmd_queue.put(("iap_done", True,
                f"校验成功: CRC={flash_crc:04X}"))
        except Exception as e:
            self.cmd_queue.put(("iap_done", False, f"校验失败: {e}"))

    def _on_iap_jump(self):
        if not self._ck(): return
        try:
            dev, app_addr = self._iap_get_params()
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        # memo removed
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_jump,
                             args=(dev, app_addr), daemon=True)
        t.start()

    def _iap_worker_jump(self, dev, app_addr):
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                              flash_delay=self._iap_flash_delay_sec(),
                              commit_delay_enabled=self.iap_commit_delay_var.get())
            iap.cmd_jump_to_app()
            self.cmd_queue.put(("iap_done", True, "跳转成功"))
        except Exception as e:
            self.cmd_queue.put(("iap_done", False, f"跳转失败: {e}"))

    def _on_iap_auto(self):
        if not self._ck(): return
        if not self.iap_bin_data:
            messagebox.showwarning("Warning", "请先选择 app.bin")
            return
        try:
            dev, app_addr = self._iap_get_params()
            chunk = self._iap_get_chunk()
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        # memo removed
        self._iap_set_buttons(False)
        self.iap_stop_event = threading.Event()
        self.pbar_iap["maximum"] = len(self.iap_bin_data)
        self.pbar_iap["value"] = 0
        t = threading.Thread(target=self._iap_worker_auto,
                             args=(dev, app_addr, chunk), daemon=True)
        t.start()

    def _iap_worker_auto(self, dev, app_addr, chunk):
        def progress(done, total):
            self.cmd_queue.put(("iap_prog", done, total))
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)),
                              flash_delay=self._iap_flash_delay_sec(),
                              commit_delay_enabled=self.iap_commit_delay_var.get())
            iap.upgrade_bytes(self.iap_bin_data, chunk_size=chunk,
                              progress_callback=progress,
                              stop_event=self.iap_stop_event)
            self.cmd_queue.put(("iap_done", True, "自动升级成功"))
        except Exception as e:
            self.cmd_queue.put(("iap_done", False, f"自动升级失败: {e}"))

    def _on_copy_batch(self):
        c = self.b_res.get(1.0, tk.END).strip()
        if c:
            self.root.clipboard_clear()
            self.root.clipboard_append(c)

    def _show_help(self):
        messagebox.showinfo("Help",
            "USB-I2C Bridge Test Tool\n\n"
            "Connect -> Single/Batch R/W -> Script\n\n"
            "Script format:\n"
            "  WR <dev> <reg> <data>...\n"
            "  RD <dev> <reg> <len>\n"
            "  SLEEP <ms>")

    def _on_close(self):
        if self.bridge:
            self.bridge.close()
        self.root.destroy()

    # ----- Utils -----

    @staticmethod
    def _vhx(v):
        if not v:
            return True
        if len(v) > 2:
            return False
        return all(c in "0123456789abcdefABCDEF" for c in v)

    @staticmethod
    def _vhx32(v):
        """Validate up to 8 hex chars (32-bit address)."""
        if not v:
            return True
        if len(v) > 8:
            return False
        return all(c in "0123456789abcdefABCDEF" for c in v)

    def _ck(self):
        if not self.bridge or not self.bridge.is_open():
            messagebox.showwarning("Warning", "Connect first")
            return False
        return True

    def _clear_log(self):
        self.log.delete(1.0, tk.END)

    def log_add(self, msg, tag="inf"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.insert(tk.END, f"[{ts}] ", "ts")
        self.log.insert(tk.END, msg + "\n", tag)
        self.log.see(tk.END)

    def _poll(self):
        try:
            while True:
                item = self.cmd_queue.get_nowait()
                t = item[0]
                if t == "log":
                    self.log_add(item[1], item[2])
                elif t == "prog":
                    self.pbar["value"] = item[1]
                    self.progress_str.set(f"{item[1]}/{len(self.script_commands)}")
                elif t == "done":
                    self.script_running = False
                    self.btn_script.config(state=tk.NORMAL)
                    self.progress_str.set("Done")
                elif t == "iap_prog":
                    done, total = item[1], item[2]
                    self.pbar_iap["value"] = done
                    self.pbar_iap.update_idletasks()
                elif t == "iap_done":
                    ok, msg = item[1], item[2]
                    self.iap_running = False
                    self._iap_set_buttons(True)
                    if ok:
                        self._iap_log_res(f"OK: {msg}")
                        self.log_add(msg, "rx")
                    else:
                        self._iap_log_res(f"ERR: {msg}")
                        self.log_add(f"IAP failed: {msg}", "err")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    I2CTestApp().run()
