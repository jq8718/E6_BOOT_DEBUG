#!/usr/bin/env python3
"""
USB-I2C Bridge GUI Test Tool for ESP32-P4
功能: 单个读写, 批量读写, 总线扫描, 脚本文件批量执行
"""

import sys, os, re, time, json, threading, queue
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
    def __init__(self, port, timeout=3.0):
        self.ser = serial.Serial(port, 115200, timeout=timeout)
        self._orig_timeout = timeout
        # Try to reduce Windows CDC ACM latency by using small buffers.
        try:
            self.ser.set_buffer_size(rx_size=64, tx_size=64)
        except Exception:
            pass
        time.sleep(0.5)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def is_open(self):
        return self.ser and self.ser.is_open

    def send_cmd(self, cmd, resp_timeout=0.5):
        """Send command, read one line response with short timeout."""
        self.ser.reset_input_buffer()
        self.ser.write((cmd.strip() + "\r\n").encode("utf-8"))
        self.ser.flush()
        orig_to = self.ser.timeout
        self.ser.timeout = resp_timeout
        try:
            result = self.ser.readline().decode("utf-8", errors="replace").strip()
        finally:
            self.ser.timeout = orig_to
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
    W = 1280
    H = 740

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("USB-I2C Bridge 测试工具 v1.0")
        self.root.geometry(f"{self.W}x{self.H}")
        self.root.minsize(1100, 650)

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
        ttk.Label(f, text="Info available in Log", foreground="gray").grid(row=6, column=0, sticky=tk.W, pady=2)

    def _build_single_tab(self, nb):
        f = self._add_tab(nb, "单个读写")
        vh = (f.register(self._vhx), "%P")

        def er(r, c, txt, **kw):
            ttk.Label(f, text=txt).grid(row=r, column=c, sticky=tk.W, **kw)

        er(0, 0, "Dev Addr (hex):", pady=4)
        self.s_dev = ttk.Entry(f, width=12, validate="key", validatecommand=vh)
        self.s_dev.insert(0, "18")
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
        self.q_dev.insert(0, "18")
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
        self.b_dev.insert(0, "18")
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
        self.iap_dev.insert(0, "20")
        self.iap_dev.grid(row=0, column=1, sticky=tk.W, padx=(2, 8))

        er(0, 2, "APP 基地址 (hex):", padx=(8, 0), pady=4)
        self.iap_addr = ttk.Entry(f, width=12, validate="key", validatecommand=vh32)
        self.iap_addr.insert(0, "2000")
        self.iap_addr.grid(row=0, column=3, sticky=tk.W, padx=(2, 8))

        er(1, 0, "每包固件数据字节数:", pady=4)
        self.iap_chunk = ttk.Entry(f, width=12)
        self.iap_chunk.insert(0, "512")
        self.iap_chunk.grid(row=1, column=1, sticky=tk.W, padx=(2, 8))

        er(1, 2, "超时(ms):", padx=(8, 0), pady=4)
        self.iap_timeout = ttk.Entry(f, width=12)
        self.iap_timeout.insert(0, "10000")
        self.iap_timeout.grid(row=1, column=3, sticky=tk.W, padx=(2, 8))

        # file selection
        self.iap_file_str = tk.StringVar(value="未选择 app.bin")
        ttk.Label(f, textvariable=self.iap_file_str, foreground="blue").grid(
            row=2, column=0, columnspan=4, sticky=tk.W, pady=4)

        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, columnspan=4, sticky=tk.W, pady=4)
        ttk.Button(bf, text="选择文件", command=self._on_iap_select_file).pack(side=tk.LEFT, padx=2)
        self.btn_iap_hs = ttk.Button(bf, text="握手", command=self._on_iap_handshake)
        self.btn_iap_hs.pack(side=tk.LEFT, padx=1)
        self.btn_iap_erase = ttk.Button(bf, text="擦除", command=self._on_iap_erase)
        self.btn_iap_erase.pack(side=tk.LEFT, padx=1)
        self.btn_iap_dl = ttk.Button(bf, text="下载", command=self._on_iap_download)
        self.btn_iap_dl.pack(side=tk.LEFT, padx=1)
        self.btn_iap_crc = ttk.Button(bf, text="校验", command=self._on_iap_crc)
        self.btn_iap_crc.pack(side=tk.LEFT, padx=1)
        self.btn_iap_jump = ttk.Button(bf, text="跳转", command=self._on_iap_jump)
        self.btn_iap_jump.pack(side=tk.LEFT, padx=1)
        self.btn_iap_auto = ttk.Button(bf, text="自动", command=self._on_iap_auto)
        self.btn_iap_auto.pack(side=tk.LEFT, padx=2)
        ttk.Button(bf, text="停止", command=self._on_iap_stop).pack(side=tk.LEFT, padx=2)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=4, column=0, columnspan=4, sticky=tk.EW, pady=8)

        ttk.Label(f, text="升级进度:", font=("", 9, "bold")).grid(row=5, column=0, sticky=tk.W)
        self.pbar_iap = ttk.Progressbar(f, mode="determinate")
        self.pbar_iap.grid(row=6, column=0, columnspan=4, sticky=tk.EW, pady=4)
        self.iap_progress_str = tk.StringVar(value="就绪")
        ttk.Label(f, textvariable=self.iap_progress_str).grid(row=7, column=0, sticky=tk.W)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=8, column=0, columnspan=4, sticky=tk.EW, pady=8)

        # bin content display
        ttk.Label(f, text="app.bin 内容 (hex):", font=("", 9, "bold")).grid(row=9, column=0, sticky=tk.W)
        self.iap_bin = tk.Text(f, height=8, width=70, font=("Consolas", 9))
        self.iap_bin.grid(row=10, column=0, columnspan=4, sticky=tk.NSEW, pady=4)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=11, column=0, columnspan=4, sticky=tk.EW, pady=8)
        ttk.Label(f, text="IAP 结果:", font=("", 9, "bold")).grid(row=12, column=0, sticky=tk.W)
        self.iap_res = tk.Text(f, height=8, width=70, font=("Consolas", 9))
        self.iap_res.grid(row=13, column=0, columnspan=4, sticky=tk.NSEW, pady=4)

        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)
        f.rowconfigure(10, weight=1)
        f.rowconfigure(13, weight=2)

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
        return IapProtocol(self.bridge, dev, app_addr,
                           log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)))

    def _iap_set_buttons(self, enabled: bool):
        state = tk.NORMAL if enabled else tk.DISABLED
        for b in (self.btn_iap_hs, self.btn_iap_erase, self.btn_iap_dl,
                  self.btn_iap_crc, self.btn_iap_jump, self.btn_iap_auto):
            b.config(state=state)

    def _iap_get_chunk(self):
        chunk = int(self.iap_chunk.get().strip())
        if chunk < 1 or chunk > 512:
            raise ValueError("每包固件数据字节数必须在 1~512")
        return chunk

    def _iap_reset_progress(self):
        self.pbar_iap["value"] = 0
        self.iap_progress_str.set("就绪")

    def _iap_log_res(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.iap_res.insert(tk.END, f"[{ts}] {msg}\n")
        self.iap_res.see(tk.END)

    # ----- IAP handlers -----

    def _on_iap_select_file(self):
        path = filedialog.askopenfilename(
            title="选择 app.bin",
            filetypes=[("BIN files", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        self.iap_file = path
        self.iap_file_str.set(path)
        try:
            with open(path, 'rb') as f:
                self.iap_bin_data = f.read()
        except Exception as e:
            messagebox.showerror("Error", f"读取文件失败: {e}")
            return
        self._iap_display_bin()
        self._iap_log_res(f"已加载 {path}, 大小 {len(self.iap_bin_data)} bytes")
        self._iap_reset_progress()

    def _iap_display_bin(self):
        self.iap_bin.delete("1.0", tk.END)
        if not self.iap_bin_data:
            return
        lines = []
        for i in range(0, len(self.iap_bin_data), 16):
            chunk = self.iap_bin_data[i:i + 16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            lines.append(f"{i:08X}: {hex_part:<48} {ascii_part}")
        self.iap_bin.insert("1.0", "\n".join(lines))

    def _on_iap_handshake(self):
        if not self._ck(): return
        try:
            dev, app_addr = self._iap_get_params()
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        self.iap_res.delete("1.0", tk.END)
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_handshake,
                             args=(dev, app_addr), daemon=True)
        t.start()

    def _iap_worker_handshake(self, dev, app_addr):
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)))
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
        self.iap_res.delete("1.0", tk.END)
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_erase,
                             args=(dev, app_addr, len(self.iap_bin_data)), daemon=True)
        t.start()

    def _iap_worker_erase(self, dev, app_addr, app_size):
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)))
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
        self.iap_res.delete("1.0", tk.END)
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
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)))
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
        self.iap_res.delete("1.0", tk.END)
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_crc,
                             args=(dev, app_addr, len(self.iap_bin_data)), daemon=True)
        t.start()

    def _iap_worker_crc(self, dev, app_addr, app_size):
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)))
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
        self.iap_res.delete("1.0", tk.END)
        self._iap_set_buttons(False)
        self._iap_reset_progress()
        t = threading.Thread(target=self._iap_worker_jump,
                             args=(dev, app_addr), daemon=True)
        t.start()

    def _iap_worker_jump(self, dev, app_addr):
        try:
            iap = IapProtocol(self.bridge, dev, app_addr,
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)))
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
        self.iap_res.delete("1.0", tk.END)
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
                              log_callback=lambda kind, msg: self.cmd_queue.put(("log", f"[IAP] {msg}", kind)))
            iap.upgrade_bytes(self.iap_bin_data, chunk_size=chunk,
                              progress_callback=progress,
                              stop_event=self.iap_stop_event)
            self.cmd_queue.put(("iap_done", True, "自动升级成功"))
        except Exception as e:
            self.cmd_queue.put(("iap_done", False, f"自动升级失败: {e}"))

    def _on_iap_stop(self):
        if self.iap_stop_event:
            self.iap_stop_event.set()
        self.log_add("IAP stop requested", "err")

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
                    self.iap_progress_str.set(f"{done} / {total} bytes ({100*done//total}%)")
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
