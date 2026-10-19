#!/usr/bin/env python3
"""
server_gui.py: Modern Desktop Control Panel for Daily Tasks
Provides process management, real-time logging terminal, LAN IP broadcasting,
and direct browser navigation shortcuts.
"""

import os
import sys
import time
import socket
import signal
import subprocess
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Ensure current directory is on sys.path
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import config
    DEFAULT_HOST = getattr(config, "HOST", "0.0.0.0")
    DEFAULT_PORT = getattr(config, "PORT", 5000)
    LAN_IP = getattr(config, "LAN_IP", "127.0.0.1")
except Exception:
    DEFAULT_HOST = "0.0.0.0"
    DEFAULT_PORT = 5000
    LAN_IP = "127.0.0.1"


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check whether a given port is currently bound/open."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, int(port))) == 0


def get_pids_on_port(port: int) -> list:
    """Find all PIDs listening on a specific port on Windows."""
    pids = set()
    if sys.platform == "win32":
        try:
            output = subprocess.check_output(
                f"netstat -ano | findstr :{port}",
                shell=True,
                text=True,
                stderr=subprocess.DEVNULL
            )
            for line in output.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in line.upper():
                    pid = parts[-1]
                    if pid.isdigit():
                        pids.add(int(pid))
        except Exception:
            pass
    return list(pids)


def kill_pids(pids: list):
    """Force kill a list of process IDs on Windows."""
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(f"taskkill /F /T /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


class OCRHubGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Daily Tasks: Server Control Panel")
        self.root.geometry("980x720")
        self.root.minsize(860, 600)

        # Apply dark theme styling
        self.bg_main = "#0F172A"       # Slate 900
        self.bg_card = "#1E293B"       # Slate 800
        self.bg_card_inner = "#334155" # Slate 700
        self.bg_terminal = "#020617"   # Slate 950
        self.fg_main = "#F8FAFC"       # Slate 50
        self.fg_muted = "#94A3B8"      # Slate 400
        self.color_primary = "#3B82F6" # Blue 500
        self.color_success = "#10B981" # Emerald 500
        self.color_danger = "#EF4444"  # Red 500
        self.color_warning = "#F59E0B" # Amber 500
        self.color_purple = "#8B5CF6"  # Violet 500

        self.root.configure(bg=self.bg_main)

        # Server state tracking
        self.server_process = None
        self.reader_thread = None
        self.monitor_thread = None
        self.is_running = False
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.auto_scroll_var = tk.BooleanVar(value=True)

        self._init_styles()
        self._build_ui()
        self._start_background_monitor()

        # Handle window close event
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _init_styles(self):
        self.style = ttk.Style()
        self.style.theme_use("clam")

        # Configure default ttk widgets
        self.style.configure(".", background=self.bg_main, foreground=self.fg_main, font=("Segoe UI", 9))
        self.style.configure("Card.TFrame", background=self.bg_card, relief="flat")
        self.style.configure("InnerCard.TFrame", background=self.bg_card_inner, relief="flat")

        # Custom buttons
        self.style.configure(
            "Primary.TButton",
            background=self.color_primary,
            foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            padding=(12, 8),
            borderwidth=0
        )
        self.style.map("Primary.TButton", background=[("active", "#2563EB")])

        self.style.configure(
            "Success.TButton",
            background=self.color_success,
            foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            padding=(14, 9),
            borderwidth=0
        )
        self.style.map("Success.TButton", background=[("active", "#059669")])

        self.style.configure(
            "Danger.TButton",
            background=self.color_danger,
            foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            padding=(14, 9),
            borderwidth=0
        )
        self.style.map("Danger.TButton", background=[("active", "#DC2626")])

        self.style.configure(
            "Warning.TButton",
            background=self.color_warning,
            foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            padding=(14, 9),
            borderwidth=0
        )
        self.style.map("Warning.TButton", background=[("active", "#D97706")])

        self.style.configure(
            "Outline.TButton",
            background=self.bg_card_inner,
            foreground=self.fg_main,
            font=("Segoe UI", 9),
            padding=(8, 5),
            borderwidth=0
        )
        self.style.map("Outline.TButton", background=[("active", "#475569")])

    def _build_ui(self):
        # 1. Top Header Banner
        header = tk.Frame(self.root, bg=self.bg_card, height=70, padx=20, pady=12)
        header.pack(fill="x", side="top")

        # App Logo & Title
        title_box = tk.Frame(header, bg=self.bg_card)
        title_box.pack(side="left")

        title_lbl = tk.Label(
            title_box,
            text="Daily Tasks",
            font=("Segoe UI", 16, "bold"),
            fg=self.fg_main,
            bg=self.bg_card
        )
        title_lbl.pack(side="left", padx=(0, 10))

        sub_lbl = tk.Label(
            title_box,
            text="Unified Server Control Panel",
            font=("Segoe UI", 10),
            fg=self.fg_muted,
            bg=self.bg_card
        )
        sub_lbl.pack(side="left")

        # Server Status Badge (Right aligned)
        status_box = tk.Frame(header, bg=self.bg_card)
        status_box.pack(side="right")

        self.status_pill = tk.Label(
            status_box,
            text="SERVER STOPPED",
            font=("Segoe UI", 10, "bold"),
            fg="#FFFFFF",
            bg=self.color_danger,
            padx=14,
            pady=5,
            relief="flat"
        )
        self.status_pill.pack(side="right")

        # 2. Main Content Container
        main_box = tk.Frame(self.root, bg=self.bg_main, padx=16, pady=12)
        main_box.pack(fill="both", expand=True)

        # Left Column: Controls, Quick Links, Info (Width: 380px)
        left_col = tk.Frame(main_box, bg=self.bg_main, width=380)
        left_col.pack(side="left", fill="y", padx=(0, 12))
        left_col.pack_propagate(False)

        # Card 1: Server Control Buttons
        ctrl_card = tk.LabelFrame(
            left_col,
            text=" Server Controls ",
            font=("Segoe UI", 10, "bold"),
            fg=self.fg_main,
            bg=self.bg_card,
            padx=14,
            pady=12,
            relief="solid",
            bd=1
        )
        ctrl_card.pack(fill="x", pady=(0, 10))

        # Main 3 Buttons: Start, Stop, Restart
        self.btn_start = tk.Button(
            ctrl_card,
            text="START SERVER",
            font=("Segoe UI", 10, "bold"),
            bg=self.color_success,
            fg="#FFFFFF",
            activebackground="#059669",
            activeforeground="#FFFFFF",
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=8,
            command=self.start_server
        )
        self.btn_start.pack(fill="x", pady=3)

        self.btn_stop = tk.Button(
            ctrl_card,
            text="STOP SERVER",
            font=("Segoe UI", 10, "bold"),
            bg=self.color_danger,
            fg="#FFFFFF",
            activebackground="#DC2626",
            activeforeground="#FFFFFF",
            relief="flat",
            cursor="hand2",
            state="disabled",
            padx=10,
            pady=8,
            command=self.stop_server
        )
        self.btn_stop.pack(fill="x", pady=3)

        self.btn_restart = tk.Button(
            ctrl_card,
            text="RESTART SERVER",
            font=("Segoe UI", 10, "bold"),
            bg=self.color_warning,
            fg="#FFFFFF",
            activebackground="#D97706",
            activeforeground="#FFFFFF",
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=8,
            command=self.restart_server
        )
        self.btn_restart.pack(fill="x", pady=3)

        # Host & Port Settings Row
        config_row = tk.Frame(ctrl_card, bg=self.bg_card, pady=6)
        config_row.pack(fill="x", pady=(4, 0))

        tk.Label(config_row, text="Host:", font=("Segoe UI", 9), fg=self.fg_muted, bg=self.bg_card).pack(side="left")
        self.entry_host = tk.Entry(
            config_row,
            textvariable=self.host_var,
            width=10,
            font=("Segoe UI", 9),
            bg=self.bg_terminal,
            fg=self.fg_main,
            insertbackground="white",
            relief="flat"
        )
        self.entry_host.pack(side="left", padx=(4, 10))

        tk.Label(config_row, text="Port:", font=("Segoe UI", 9), fg=self.fg_muted, bg=self.bg_card).pack(side="left")
        self.entry_port = tk.Entry(
            config_row,
            textvariable=self.port_var,
            width=6,
            font=("Segoe UI", 9),
            bg=self.bg_terminal,
            fg=self.fg_main,
            insertbackground="white",
            relief="flat"
        )
        self.entry_port.pack(side="left", padx=(4, 0))

        # Kill orphan port button
        btn_free_port = tk.Button(
            ctrl_card,
            text="Free Stuck Port 5000",
            font=("Segoe UI", 8),
            bg=self.bg_card_inner,
            fg=self.fg_muted,
            activebackground="#475569",
            activeforeground="#FFFFFF",
            relief="flat",
            cursor="hand2",
            pady=3,
            command=self.free_port
        )
        btn_free_port.pack(fill="x", pady=(6, 0))

        # Card 2: Quick Links / Open Web Apps
        apps_card = tk.LabelFrame(
            left_col,
            text=" Quick Access (Open In Browser) ",
            font=("Segoe UI", 10, "bold"),
            fg=self.fg_main,
            bg=self.bg_card,
            padx=12,
            pady=10,
            relief="solid",
            bd=1
        )
        apps_card.pack(fill="x", pady=(0, 10))

        routes = [
            ("Dashboard", "/dashboard", self.color_primary),
            ("File Share", "/filesharing", "#16A34A"),
            ("PAN Card OCR", "/pan", "#0284C7"),
            ("Aadhaar Card OCR", "/aadhar", "#0D9488"),
            ("Policy Reader", "/policy", self.color_purple),
            ("LP_Delete Tool", "/lp-delete", "#E11D48"),
            ("LP_DateWise Sampling", "/lp-datewise", "#D97706"),
        ]

        for i, (label, route, color) in enumerate(routes):
            btn = tk.Button(
                apps_card,
                text=label,
                font=("Segoe UI", 9, "bold"),
                bg=self.bg_card_inner,
                fg=self.fg_main,
                activebackground=color,
                activeforeground="#FFFFFF",
                relief="flat",
                cursor="hand2",
                anchor="w",
                padx=10,
                pady=4,
                command=lambda r=route: self.open_browser(r)
            )
            btn.pack(fill="x", pady=2)

        # Card 3: Folders & System Info
        info_card = tk.LabelFrame(
            left_col,
            text=" Folders & Info ",
            font=("Segoe UI", 10, "bold"),
            fg=self.fg_main,
            bg=self.bg_card,
            padx=12,
            pady=8,
            relief="solid",
            bd=1
        )
        info_card.pack(fill="both", expand=True)

        # System info labels
        self.lbl_local_url = tk.Label(
            info_card,
            text=f"Local: http://localhost:{DEFAULT_PORT}",
            font=("Segoe UI", 8),
            fg=self.fg_muted,
            bg=self.bg_card,
            anchor="w"
        )
        self.lbl_local_url.pack(fill="x")

        self.lbl_lan_url = tk.Label(
            info_card,
            text=f"LAN: http://{LAN_IP}:{DEFAULT_PORT}",
            font=("Segoe UI", 8),
            fg=self.fg_muted,
            bg=self.bg_card,
            anchor="w"
        )
        self.lbl_lan_url.pack(fill="x")

        # Copy LAN URL button
        tk.Button(
            info_card,
            text="Copy LAN Address",
            font=("Segoe UI", 8),
            bg=self.bg_card_inner,
            fg=self.fg_main,
            relief="flat",
            cursor="hand2",
            pady=2,
            command=self.copy_lan_url
        ).pack(fill="x", pady=(4, 6))

        folder_row = tk.Frame(info_card, bg=self.bg_card, pady=2)
        folder_row.pack(fill="x")

        tk.Button(
            folder_row,
            text="Outputs",
            font=("Segoe UI", 8),
            bg=self.bg_card_inner,
            fg=self.fg_main,
            relief="flat",
            cursor="hand2",
            padx=4,
            pady=3,
            command=lambda: self.open_folder("outputs")
        ).pack(side="left", padx=(0, 3), expand=True, fill="x")

        tk.Button(
            folder_row,
            text="Uploads",
            font=("Segoe UI", 8),
            bg=self.bg_card_inner,
            fg=self.fg_main,
            relief="flat",
            cursor="hand2",
            padx=4,
            pady=3,
            command=lambda: self.open_folder("uploads")
        ).pack(side="left", padx=(0, 3), expand=True, fill="x")

        tk.Button(
            folder_row,
            text="Fileshare",
            font=("Segoe UI", 8),
            bg=self.bg_card_inner,
            fg=self.fg_main,
            relief="flat",
            cursor="hand2",
            padx=4,
            pady=3,
            command=lambda: self.open_folder("fileshare_uploads")
        ).pack(side="left", padx=(0, 3), expand=True, fill="x")

        tk.Button(
            folder_row,
            text="Logs",
            font=("Segoe UI", 8),
            bg=self.bg_card_inner,
            fg=self.fg_main,
            relief="flat",
            cursor="hand2",
            padx=4,
            pady=3,
            command=lambda: self.open_folder("log")
        ).pack(side="left", expand=True, fill="x")

        # Right Column: Real-time Live Log Console
        right_col = tk.Frame(main_box, bg=self.bg_main)
        right_col.pack(side="right", fill="both", expand=True)

        log_card = tk.LabelFrame(
            right_col,
            text=" Live Server Console Output ",
            font=("Segoe UI", 10, "bold"),
            fg=self.fg_main,
            bg=self.bg_card,
            padx=10,
            pady=8,
            relief="solid",
            bd=1
        )
        log_card.pack(fill="both", expand=True)

        # Log toolbar
        log_toolbar = tk.Frame(log_card, bg=self.bg_card, pady=4)
        log_toolbar.pack(fill="x")

        tk.Checkbutton(
            log_toolbar,
            text="Auto-scroll",
            variable=self.auto_scroll_var,
            font=("Segoe UI", 8),
            fg=self.fg_main,
            bg=self.bg_card,
            activebackground=self.bg_card,
            activeforeground=self.fg_main,
            selectcolor=self.bg_terminal
        ).pack(side="left")

        tk.Button(
            log_toolbar,
            text="Copy All",
            font=("Segoe UI", 8),
            bg=self.bg_card_inner,
            fg=self.fg_main,
            relief="flat",
            cursor="hand2",
            padx=8,
            pady=2,
            command=self.copy_logs
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            log_toolbar,
            text="Clear Console",
            font=("Segoe UI", 8),
            bg=self.bg_card_inner,
            fg=self.fg_main,
            relief="flat",
            cursor="hand2",
            padx=8,
            pady=2,
            command=self.clear_logs
        ).pack(side="right")

        # Terminal Text View with Scrollbar
        console_frame = tk.Frame(log_card, bg=self.bg_terminal)
        console_frame.pack(fill="both", expand=True, pady=(4, 0))

        self.log_text = tk.Text(
            console_frame,
            bg=self.bg_terminal,
            fg="#A7F3D0",  # Soft emerald/green text
            font=("Consolas", 9),
            wrap="word",
            padx=8,
            pady=8,
            relief="flat",
            state="normal"
        )
        self.log_scroll = ttk.Scrollbar(console_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=self.log_scroll.set)

        self.log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # Log color tags
        self.log_text.tag_config("info", foreground="#60A5FA")    # Blue
        self.log_text.tag_config("success", foreground="#34D399") # Green
        self.log_text.tag_config("warning", foreground="#FBBF24") # Yellow
        self.log_text.tag_config("error", foreground="#F87171")   # Red
        self.log_text.tag_config("dim", foreground="#64748B")     # Gray

        self.append_log("[SYSTEM] Control Panel initialized. Ready to start Daily Tasks.\n", "info")
        self.append_log(f"[SYSTEM] Workspace Root: {ROOT_DIR}\n", "dim")

    def append_log(self, text: str, tag: str = None):
        """Append line to the log text area safely in UI thread, filtering spam."""
        # Suppress routine dashboard polling and static asset requests
        ignored_patterns = ("/api/logs", "/api/stats", "/static/", "/favicon.ico")
        if any(pat in text for pat in ignored_patterns):
            return

        def _insert():
            self.log_text.configure(state="normal")
            if tag:
                self.log_text.insert("end", text, tag)
            else:
                # Intelligent line parsing for coloring
                low = text.lower()
                if "[error" in low or "exception" in low or "fail" in low or "500" in low or "traceback" in low:
                    self.log_text.insert("end", text, "error")
                elif "[warn" in low or "404" in low or "400" in low or "warning" in low:
                    self.log_text.insert("end", text, "warning")
                elif "200 ok" in low or "running on" in low or "started" in low or "completed" in low or "finished" in low or "===" in text:
                    self.log_text.insert("end", text, "success")
                elif "[pan ocr" in low or "[aadhaar ocr" in low or "[policy reader" in low or "[lp-" in low or "[request]" in low or "[launcher]" in low or "[browser]" in low:
                    self.log_text.insert("end", text, "info")
                else:
                    self.log_text.insert("end", text)

            if self.auto_scroll_var.get():
                self.log_text.see("end")
            self.log_text.configure(state="normal")

        self.root.after(0, _insert)

    def clear_logs(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.append_log("[CONSOLE] Cleared.\n", "dim")

    def copy_logs(self):
        text = self.log_text.get("1.0", "end")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        messagebox.showinfo("Copied", "Console output copied to clipboard!")

    def start_server(self):
        """Start the Flask OCR Hub server in background thread."""
        if self.is_running or (self.server_process and self.server_process.poll() is None):
            messagebox.showinfo("Notice", "Server is already running!")
            return

        port = self.port_var.get().strip()
        host = self.host_var.get().strip()
        if not port.isdigit():
            messagebox.showerror("Error", "Invalid Port number. Please enter a valid number (e.g. 5000).")
            return

        port_num = int(port)
        if is_port_in_use(port_num):
            pids = get_pids_on_port(port_num)
            resp = messagebox.askyesno(
                "Port in Use",
                f"Port {port_num} is already in use by PID(s): {pids or 'Unknown'}.\n\nDo you want to terminate that process and start Daily Tasks?"
            )
            if resp:
                self.free_port(show_msg=False)
                time.sleep(1)
            else:
                return

        # Python interpreter in virtual environment or fallback
        venv_py = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if not venv_py.exists():
            venv_py = ROOT_DIR / ".venv" / "bin" / "python"
        py_exe = str(venv_py) if venv_py.exists() else sys.executable

        app_py = ROOT_DIR / "app.py"
        if not app_py.exists():
            messagebox.showerror("Error", f"Could not find app.py at {app_py}")
            return

        self.append_log(f"\n[LAUNCHER] Starting Daily Tasks (Host: {host}, Port: {port_num})...\n", "info")
        self.append_log(f"[LAUNCHER] Using Python: {py_exe}\n", "dim")

        try:
            # Set environment variables
            env = os.environ.copy()
            env["HOST"] = host
            env["PORT"] = str(port_num)
            env["PYTHONUNBUFFERED"] = "1"

            extra_kwargs = {}
            if sys.platform == "win32":
                extra_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

            self.server_process = subprocess.Popen(
                [py_exe, str(app_py), "--host", host, "--port", str(port_num)],
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
                **extra_kwargs
            )

            self.is_running = True
            self._update_status_ui(running=True)

            # Start thread to read stdout
            self.reader_thread = threading.Thread(target=self._stream_process_output, daemon=True)
            self.reader_thread.start()

        except Exception as e:
            self.is_running = False
            self._update_status_ui(running=False)
            messagebox.showerror("Error Starting Server", f"Failed to launch app.py:\n{str(e)}")
            self.append_log(f"[ERROR] {str(e)}\n", "error")

    def _stream_process_output(self):
        """Read stdout from server process and stream to GUI."""
        if not self.server_process:
            return
        try:
            for line in iter(self.server_process.stdout.readline, ""):
                if not line:
                    break
                self.append_log(line)
        except Exception:
            pass
        finally:
            if self.server_process:
                self.server_process.stdout.close()

    def stop_server(self, quiet=False):
        """Stop the running server process."""
        if not self.is_running and not (self.server_process and self.server_process.poll() is None):
            if not quiet:
                messagebox.showinfo("Notice", "Server is not running.")
            return

        self.append_log("\n[LAUNCHER] Stopping server...\n", "warning")

        if self.server_process:
            try:
                pid = self.server_process.pid
                if sys.platform == "win32":
                    subprocess.run(
                        f"taskkill /F /T /PID {pid}",
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=0x08000000
                    )
                else:
                    self.server_process.terminate()
                    self.server_process.wait(timeout=2)
            except Exception:
                try:
                    self.server_process.kill()
                except Exception:
                    pass

        # Also double check port 5000
        try:
            port_num = int(self.port_var.get())
            pids = get_pids_on_port(port_num)
            if pids:
                kill_pids(pids)
        except Exception:
            pass

        self.server_process = None
        self.is_running = False
        self._update_status_ui(running=False)
        self.append_log("[LAUNCHER] Server stopped successfully.\n", "success")

    def restart_server(self):
        """Cleanly stop and restart the server."""
        self.append_log("\n[LAUNCHER] Restarting server...\n", "info")
        self.stop_server(quiet=True)
        self.root.after(1200, self.start_server)

    def free_port(self, show_msg=True):
        """Terminate any background process locking port 5000."""
        try:
            port = int(self.port_var.get())
            pids = get_pids_on_port(port)
            if pids:
                kill_pids(pids)
                self.append_log(f"[PORT] Terminated process(es) on port {port}: {pids}\n", "warning")
                if show_msg:
                    messagebox.showinfo("Port Cleaned", f"Successfully cleared port {port} (Terminated PIDs: {pids})")
            else:
                if show_msg:
                    messagebox.showinfo("Port Free", f"Port {port} is not occupied by any process.")
        except Exception as e:
            if show_msg:
                messagebox.showerror("Error", f"Failed to free port: {e}")

    def _update_status_ui(self, running: bool):
        """Update buttons and status badge."""
        if running:
            self.status_pill.configure(text="● SERVER RUNNING (ONLINE)", bg=self.color_success)
            self.btn_start.configure(state="disabled", bg=self.bg_card_inner)
            self.btn_stop.configure(state="normal", bg=self.color_danger)
            self.btn_restart.configure(state="normal", bg=self.color_warning)
        else:
            self.status_pill.configure(text="● SERVER STOPPED (OFFLINE)", bg=self.color_danger)
            self.btn_start.configure(state="normal", bg=self.color_success)
            self.btn_stop.configure(state="disabled", bg=self.bg_card_inner)
            self.btn_restart.configure(state="normal", bg=self.color_warning)

    def _start_background_monitor(self):
        """Periodic background status checker to keep status pill accurate."""
        def _check():
            while True:
                try:
                    port = int(self.port_var.get())
                    active = is_port_in_use(port)
                    if active and not self.is_running:
                        # Server running outside GUI or started externally
                        self.root.after(0, lambda: self._update_status_ui(running=True))
                        self.is_running = True
                    elif not active and self.is_running:
                        # Server exited or crashed
                        self.root.after(0, lambda: self._update_status_ui(running=False))
                        self.is_running = False
                except Exception:
                    pass
                time.sleep(2)

        self.monitor_thread = threading.Thread(target=_check, daemon=True)
        self.monitor_thread.start()

    def open_browser(self, route: str = ""):
        """Open web browser to specified route."""
        port = self.port_var.get()
        url = f"http://localhost:{port}{route}"
        self.append_log(f"[BROWSER] Opening {url}\n", "info")
        webbrowser.open(url)

    def open_folder(self, folder_name: str):
        """Open a project folder in Windows Explorer."""
        folder_path = ROOT_DIR / folder_name
        folder_path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(folder_path))
        else:
            subprocess.run(["xdg-open", str(folder_path)])

    def copy_lan_url(self):
        """Copy LAN URL to clipboard."""
        port = self.port_var.get().strip() or "5000"
        lan_url = f"http://{LAN_IP}:{port}"
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(lan_url)
            self.append_log(f"[CLIPBOARD] Copied LAN URL: {lan_url}\n", "info")
            messagebox.showinfo("Copied", f"LAN URL copied to clipboard:\n\n{lan_url}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to copy to clipboard: {e}")

    def on_close(self):
        """Handle window closing."""
        if self.server_process and self.server_process.poll() is None:
            resp = messagebox.askyesno(
                "Exit Control Panel",
                "Daily Tasks server is still running.\n\nDo you want to stop the server before exiting?"
            )
            if resp:
                self.stop_server(quiet=True)
        self.root.destroy()


def main():
    try:
        root = tk.Tk()
        app = OCRHubGUI(root)
        root.mainloop()
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        try:
            messagebox.showerror("Daily Tasks GUI Error", f"An error occurred while launching GUI:\n\n{err_msg}")
        except Exception:
            pass
        try:
            with open(ROOT_DIR / "gui_error.log", "w", encoding="utf-8") as f:
                f.write(err_msg)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
