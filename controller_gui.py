#!/usr/bin/env python3
"""
Controller GUI — pairing-server identity, triggers, voice, and sensor stream.

Desktop UI for the controller machine (separate from the pairing web app).
"""

from __future__ import annotations

import json
import os
import queue
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from dotenv import load_dotenv

from actions import LovenseError, LovenseSocketClient
from config import (
    DEFAULT_ACTION,
    DEFAULT_COOLDOWN,
    DEFAULT_TIME_SEC,
    DEFAULT_TRIGGERS_FILE,
    DEFAULT_UNAME,
    ENV_PATH,
    env,
    upsert_env_value,
)
from controller_client import (
    ControllerConfigError,
    fetch_owner_identity,
    fetch_pairing_health,
)
from recognition import RecognitionError, VoiceListener, list_input_device_choices
from sensor_stream import (
    DEFAULT_UDP_PORT,
    RecordingError,
    SensorRecorder,
    SensorReplay,
    UdpSensorReceiver,
    load_recording_csv,
    map_value_to_level,
)
from triggers import (
    ActionDefaults,
    TriggerAction,
    TriggerConfigError,
    TriggerEngine,
    expand_entries_to_actions,
    load_triggers_document,
    normalize_trigger_entries,
    resolve_config_path,
    save_triggers_document,
)


class TriggerEditDialog(tk.Toplevel):
    """Modal editor for one trigger entry (phrases + Lovense output)."""

    def __init__(self, master: tk.Misc, title: str, initial: dict | None = None) -> None:
        super().__init__(master)
        self.title(title)
        self.configure(bg="#12141a")
        self.resizable(True, False)
        self.transient(master)
        self.grab_set()
        self.result: dict | None = None

        initial = initial or {}
        phrases = initial.get("phrases") or []
        self.var_phrases = tk.StringVar(value=", ".join(phrases))
        self.var_action = tk.StringVar(value=str(initial.get("action") or DEFAULT_ACTION))
        self.var_time = tk.StringVar(value=str(initial.get("timeSec", DEFAULT_TIME_SEC)))
        self.var_cooldown = tk.StringVar(value=str(initial.get("cooldown", DEFAULT_COOLDOWN)))
        self.var_toy = tk.StringVar(value=str(initial.get("toy") or ""))

        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        rows = [
            ("Phrases (comma-separated)", self.var_phrases),
            ("Action (e.g. Vibrate:16 or Stop)", self.var_action),
            ("Duration timeSec", self.var_time),
            ("Cooldown (seconds)", self.var_cooldown),
            ("Toy id (optional)", self.var_toy),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky=tk.W, pady=4)
            ttk.Entry(body, textvariable=var, width=48).grid(
                row=i, column=1, sticky=tk.EW, pady=4, padx=(8, 0)
            )
        body.columnconfigure(1, weight=1)

        btns = ttk.Frame(body)
        btns.grid(row=len(rows), column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btns, text="OK", command=self._ok).pack(side=tk.RIGHT)

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.wait_visibility()
        self.focus()

    def _ok(self) -> None:
        phrases = [p.strip() for p in self.var_phrases.get().split(",") if p.strip()]
        if not phrases:
            messagebox.showwarning("Invalid", "Enter at least one phrase.", parent=self)
            return
        action = self.var_action.get().strip()
        if not action:
            messagebox.showwarning("Invalid", "Action is required.", parent=self)
            return
        try:
            time_sec = float(self.var_time.get().strip() or DEFAULT_TIME_SEC)
            cooldown = float(self.var_cooldown.get().strip() or DEFAULT_COOLDOWN)
        except ValueError:
            messagebox.showwarning(
                "Invalid", "timeSec and cooldown must be numbers.", parent=self
            )
            return
        entry: dict[str, Any] = {
            "phrases": phrases,
            "action": action,
            "timeSec": time_sec,
            "cooldown": cooldown,
        }
        toy = self.var_toy.get().strip()
        if toy:
            entry["toy"] = toy
        self.result = entry
        self.destroy()


class ControllerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Buzz Lightyear — Controller")
        self.minsize(720, 640)
        self.geometry("820x720")

        self._identity: dict[str, Any] | None = None
        self._entries: list[dict] = []
        self._defaults: dict[str, Any] = {
            "action": DEFAULT_ACTION,
            "timeSec": DEFAULT_TIME_SEC,
            "cooldown": DEFAULT_COOLDOWN,
        }
        self._triggers_path = resolve_config_path(
            env("TRIGGERS_FILE", str(DEFAULT_TRIGGERS_FILE)) or str(DEFAULT_TRIGGERS_FILE)
        )
        self._client: LovenseSocketClient | None = None
        self._listener: VoiceListener | None = None
        self._engine: TriggerEngine | None = None
        self._ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

        # Sensor (UDP / replay) stream state
        self._sensor_receiver: UdpSensorReceiver | SensorReplay | None = None
        self._sensor_recorder: SensorRecorder | None = None
        self._sensor_control_active = False
        self._sensor_last_level: int | None = None
        self._sensor_last_send_mono = 0.0
        self._sensor_commands_sent = 0
        self._notebook: ttk.Notebook | None = None

        self._build_style()
        self._build_ui()
        self._load_from_env()
        self._load_triggers_file(silent=True)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._drain_ui_queue)
        self.after(50, self._sensor_tick)

    # --- style / layout -----------------------------------------------------

    def _build_style(self) -> None:
        # Full dark palette — clam keeps light defaults for buttons/tabs/etc.
        # unless every surface + map state is set (light text on light bg otherwise).
        bg = "#12141a"
        card = "#1a1e28"
        field = "#0c0e13"
        text = "#e8ecf4"
        muted = "#9aa3b2"
        accent = "#ffc14d"
        button = "#2a3142"
        button_hover = "#3a455c"
        button_pressed = "#1e2430"
        button_disabled = "#1a1e28"
        disabled_fg = "#6b7380"
        select_bg = "#3d4f73"
        tab = "#1e2430"
        tab_selected = "#2a3142"
        border = "#3a4254"

        self.configure(bg=bg)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".",
            background=bg,
            foreground=text,
            fieldbackground=field,
            troughcolor=card,
            bordercolor=border,
            darkcolor=card,
            lightcolor=card,
            insertcolor=text,
            selectbackground=select_bg,
            selectforeground=text,
        )
        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=card)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=tab,
            foreground=text,
            padding=[12, 6],
            borderwidth=1,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", tab_selected), ("active", button_hover)],
            foreground=[("selected", accent), ("active", text), ("disabled", disabled_fg)],
        )
        style.configure("TLabel", background=bg, foreground=text)
        style.configure("Card.TLabel", background=card, foreground=text)
        style.configure("Muted.TLabel", background=card, foreground=muted)
        style.configure(
            "Title.TLabel",
            background=bg,
            foreground=accent,
            font=("Segoe UI", 14, "bold"),
        )
        style.configure(
            "TButton",
            background=button,
            foreground=text,
            padding=8,
            borderwidth=1,
            focusthickness=1,
            focuscolor=accent,
        )
        style.map(
            "TButton",
            background=[
                ("disabled", button_disabled),
                ("pressed", button_pressed),
                ("active", button_hover),
            ],
            foreground=[
                ("disabled", disabled_fg),
                ("pressed", text),
                ("active", text),
            ],
            bordercolor=[("focus", accent), ("!focus", border)],
        )
        style.configure(
            "TEntry",
            fieldbackground=field,
            foreground=text,
            insertcolor=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            padding=4,
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", card), ("readonly", card)],
            foreground=[("disabled", disabled_fg)],
            bordercolor=[("focus", accent)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=field,
            background=button,
            foreground=text,
            arrowcolor=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", field),
                ("disabled", card),
                ("!disabled", field),
            ],
            foreground=[
                ("disabled", disabled_fg),
                ("readonly", text),
                ("!disabled", text),
            ],
            background=[("active", button_hover), ("!disabled", button)],
            arrowcolor=[("disabled", disabled_fg), ("!disabled", text)],
            bordercolor=[("focus", accent), ("!focus", border)],
        )
        # Dropdown list (popdown) is a separate tk Listbox; set via option_add.
        self.option_add("*TCombobox*Listbox.background", field)
        self.option_add("*TCombobox*Listbox.foreground", text)
        self.option_add("*TCombobox*Listbox.selectBackground", select_bg)
        self.option_add("*TCombobox*Listbox.selectForeground", text)
        # Prefer card bg: most checkbuttons sit inside LabelFrames / card frames.
        style.configure(
            "TCheckbutton",
            background=card,
            foreground=text,
            indicatorbackground=field,
            indicatorforeground=accent,
            focuscolor=card,
        )
        style.map(
            "TCheckbutton",
            background=[("active", card), ("selected", card), ("!disabled", card)],
            foreground=[("disabled", disabled_fg), ("!disabled", text)],
            indicatorbackground=[
                ("selected", button),
                ("active", field),
                ("disabled", card),
            ],
            indicatorforeground=[("selected", accent), ("!selected", muted)],
        )
        style.configure(
            "TLabelframe",
            background=card,
            foreground=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=card,
            foreground=accent,
        )
        style.configure(
            "Treeview",
            background=field,
            fieldbackground=field,
            foreground=text,
            bordercolor=border,
            rowheight=24,
        )
        style.configure(
            "Treeview.Heading",
            background=card,
            foreground=accent,
            bordercolor=border,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", select_bg)],
            foreground=[("selected", text)],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", button)],
            foreground=[("active", accent)],
        )
        style.configure(
            "Vertical.TScrollbar",
            background=button,
            troughcolor=card,
            bordercolor=card,
            arrowcolor=text,
        )
        style.configure(
            "Horizontal.TScrollbar",
            background=button,
            troughcolor=card,
            bordercolor=card,
            arrowcolor=text,
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", button_hover), ("disabled", card)],
            arrowcolor=[("disabled", disabled_fg)],
        )
        style.map(
            "Horizontal.TScrollbar",
            background=[("active", button_hover), ("disabled", card)],
            arrowcolor=[("disabled", disabled_fg)],
        )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer, text="Controller", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            outer,
            text="Connect to the pairing server, configure phrase → Lovense "
            "outputs, use voice listening, or map the ESP32 UDP sensor stream.",
            wraplength=780,
        ).pack(anchor=tk.W, pady=(4, 10))

        self.var_ready = tk.StringVar(
            value="Ready for control: no (fetch a paired identity first)"
        )
        ttk.Label(outer, textvariable=self.var_ready).pack(anchor=tk.W, pady=(0, 8))

        nb = ttk.Notebook(outer)
        nb.pack(fill=tk.BOTH, expand=True)
        self._notebook = nb

        tab_conn = ttk.Frame(nb, padding=10)
        tab_trig = ttk.Frame(nb, padding=10)
        tab_voice = ttk.Frame(nb, padding=10)
        tab_sensor = ttk.Frame(nb, padding=10)
        nb.add(tab_conn, text="Connection")
        nb.add(tab_trig, text="Triggers")
        nb.add(tab_voice, text="Voice")
        nb.add(tab_sensor, text="Sensor")

        self._build_connection_tab(tab_conn)
        self._build_triggers_tab(tab_trig)
        self._build_voice_tab(tab_voice)
        self._build_sensor_tab(tab_sensor)
        nb.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

    def _build_connection_tab(self, parent: ttk.Frame) -> None:
        conn = ttk.LabelFrame(parent, text="Pairing server connection", padding=12)
        conn.pack(fill=tk.X, pady=(0, 10))

        self.var_server = tk.StringVar()
        self.var_api_key = tk.StringVar()
        self.var_token = tk.StringVar()
        self.var_timeout = tk.StringVar(value="10")

        self._row_entry(
            conn, 0, "Pairing server URL", self.var_server,
            hint="e.g. https://pair.example.com",
        )
        self._row_entry(conn, 1, "Controller API key", self.var_api_key, show="•")
        self._row_entry(
            conn, 2, "Lovense developer token", self.var_token, show="•",
            hint="Local only — never fetched from the pairing server",
        )
        self._row_entry(conn, 3, "Request timeout (s)", self.var_timeout)

        btn_row = ttk.Frame(conn, style="Card.TFrame")
        btn_row.grid(row=4, column=0, columnspan=2, sticky=tk.EW, pady=(12, 0))
        ttk.Button(btn_row, text="Test connection", command=self.on_test).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_row, text="Fetch identity", command=self.on_fetch).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_row, text="Save settings", command=self.on_save).pack(
            side=tk.LEFT
        )

        ident = ttk.LabelFrame(
            parent, text="Owner identity (from pairing server)", padding=12
        )
        ident.pack(fill=tk.BOTH, expand=True)

        self.var_status = tk.StringVar(value="Not fetched yet")
        self.var_paired = tk.StringVar(value="—")
        self.var_uid = tk.StringVar(value="—")
        self.var_platform = tk.StringVar(value="—")
        self.var_uname = tk.StringVar(value="—")
        self.var_paired_at = tk.StringVar(value="—")

        self._row_readonly(ident, 0, "Status", self.var_status)
        self._row_readonly(ident, 1, "Paired", self.var_paired)
        self._row_readonly(ident, 2, "UID", self.var_uid)
        self._row_readonly(ident, 3, "Platform", self.var_platform)
        self._row_readonly(ident, 4, "Display name", self.var_uname)
        self._row_readonly(ident, 5, "Paired at", self.var_paired_at)

        ttk.Label(ident, text="Raw response", style="Muted.TLabel").grid(
            row=6, column=0, sticky=tk.NW, pady=(10, 0)
        )
        raw_frame = ttk.Frame(ident, style="Card.TFrame")
        raw_frame.grid(row=6, column=1, sticky=tk.NSEW, pady=(10, 0))
        ident.columnconfigure(1, weight=1)
        ident.rowconfigure(6, weight=1)

        self.txt_raw = tk.Text(
            raw_frame,
            height=8,
            wrap=tk.WORD,
            bg="#0c0e13",
            fg="#c5d0e0",
            insertbackground="#e8ecf4",
            relief=tk.FLAT,
            font=("Consolas", 9),
        )
        scroll = ttk.Scrollbar(raw_frame, command=self.txt_raw.yview)
        self.txt_raw.configure(yscrollcommand=scroll.set)
        self.txt_raw.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_triggers_tab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, pady=(0, 8))

        self.var_triggers_path = tk.StringVar(value=str(self._triggers_path))
        ttk.Label(top, text="Config file").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.var_triggers_path).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=8
        )
        ttk.Button(top, text="Browse…", command=self.on_browse_triggers).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(top, text="Reload", command=lambda: self._load_triggers_file()).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(top, text="Save", command=self.on_save_triggers).pack(side=tk.LEFT)

        defaults = ttk.LabelFrame(parent, text="Defaults (used when a row omits fields)", padding=10)
        defaults.pack(fill=tk.X, pady=(0, 8))
        self.var_def_action = tk.StringVar(value=DEFAULT_ACTION)
        self.var_def_time = tk.StringVar(value=str(DEFAULT_TIME_SEC))
        self.var_def_cooldown = tk.StringVar(value=str(DEFAULT_COOLDOWN))
        self._row_entry(defaults, 0, "Default action", self.var_def_action)
        self._row_entry(defaults, 1, "Default timeSec", self.var_def_time)
        self._row_entry(defaults, 2, "Default cooldown", self.var_def_cooldown)

        table_frame = ttk.LabelFrame(parent, text="Phrase → Lovense output", padding=8)
        table_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("phrases", "action", "timeSec", "cooldown", "toy")
        self.tree = ttk.Treeview(
            table_frame, columns=cols, show="headings", selectmode="browse"
        )
        headings = {
            "phrases": "Phrases",
            "action": "Action",
            "timeSec": "timeSec",
            "cooldown": "Cooldown",
            "toy": "Toy",
        }
        widths = {"phrases": 280, "action": 120, "timeSec": 70, "cooldown": 70, "toy": 80}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], stretch=(c == "phrases"))
        yscroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda _e: self.on_edit_trigger())

        row_btns = ttk.Frame(parent)
        row_btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(row_btns, text="Add", command=self.on_add_trigger).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_btns, text="Edit", command=self.on_edit_trigger).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_btns, text="Remove", command=self.on_remove_trigger).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_btns, text="Duplicate", command=self.on_duplicate_trigger).pack(
            side=tk.LEFT
        )

    def _build_voice_tab(self, parent: ttk.Frame) -> None:
        cfg = ttk.LabelFrame(parent, text="Recognition", padding=12)
        cfg.pack(fill=tk.X, pady=(0, 8))

        self.var_model = tk.StringVar()
        self.var_device = tk.StringVar(value="default")
        self.var_test_mode = tk.BooleanVar(value=False)
        self.var_verbose = tk.BooleanVar(value=False)

        self._row_entry(
            cfg, 0, "Vosk model path", self.var_model,
            hint="Unpacked model directory (vosk-model-small-en-us-…)",
        )
        ttk.Button(cfg, text="Browse model…", command=self.on_browse_model).grid(
            row=0, column=2, padx=(8, 0)
        )

        ttk.Label(cfg, text="Input device", style="Card.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=4
        )
        self.cmb_device = ttk.Combobox(cfg, textvariable=self.var_device, width=50)
        self.cmb_device.grid(row=1, column=1, sticky=tk.EW, pady=4)
        ttk.Button(cfg, text="Refresh devices", command=self._refresh_devices).grid(
            row=1, column=2, padx=(8, 0)
        )
        cfg.columnconfigure(1, weight=1)

        opts = ttk.Frame(cfg, style="Card.TFrame")
        opts.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        ttk.Checkbutton(
            opts, text="Test mode (log only, no Lovense emit)",
            variable=self.var_test_mode,
        ).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Checkbutton(
            opts, text="Verbose transcripts", variable=self.var_verbose
        ).pack(side=tk.LEFT)

        controls = ttk.Frame(parent)
        controls.pack(fill=tk.X, pady=(0, 8))
        self.btn_start = ttk.Button(
            controls, text="Start listening", command=self.on_start_listening
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_stop = ttk.Button(
            controls, text="Stop", command=self.on_stop_listening, state=tk.DISABLED
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))
        self.var_listen_state = tk.StringVar(value="Stopped")
        ttk.Label(controls, textvariable=self.var_listen_state).pack(side=tk.LEFT)

        log_box = ttk.LabelFrame(parent, text="Activity log", padding=8)
        log_box.pack(fill=tk.BOTH, expand=True)
        self.txt_log = tk.Text(
            log_box,
            height=16,
            wrap=tk.WORD,
            bg="#0c0e13",
            fg="#c5d0e0",
            insertbackground="#e8ecf4",
            relief=tk.FLAT,
            font=("Consolas", 9),
        )
        log_scroll = ttk.Scrollbar(log_box, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=log_scroll.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._refresh_devices()

    def _build_sensor_tab(self, parent: ttk.Frame) -> None:
        intro = ttk.Label(
            parent,
            text="Selecting this tab starts the UDP receiver and connects to the "
            "Lovense Socket API (when identity + token are ready). Press "
            "Begin control to map the live 0–1 motion stream to vibration. "
            "Record live samples to CSV and replay them later without an ESP32.",
            wraplength=760,
        )
        intro.pack(anchor=tk.W, pady=(0, 8))

        cfg = ttk.LabelFrame(parent, text="UDP stream", padding=12)
        cfg.pack(fill=tk.X, pady=(0, 8))

        self.var_udp_port = tk.StringVar(
            value=env("UDP_SENSOR_PORT", str(DEFAULT_UDP_PORT)) or str(DEFAULT_UDP_PORT)
        )
        self.var_sensor_max_level = tk.StringVar(
            value=env("SENSOR_MAX_LEVEL", "20") or "20"
        )
        self.var_sensor_cmd_hz = tk.StringVar(
            value=env("SENSOR_CMD_HZ", "10") or "10"
        )
        self.var_sensor_deadband = tk.StringVar(
            value=env("SENSOR_DEADBAND", "0.02") or "0.02"
        )
        self.var_sensor_time_sec = tk.StringVar(
            value=env("SENSOR_TIME_SEC", "1.0") or "1.0"
        )
        self.var_sensor_test = tk.BooleanVar(value=False)

        self._row_entry(
            cfg, 0, "UDP listen port", self.var_udp_port,
            hint="Must match ESP32 setup form / UDP_PORT (default 5005)",
        )
        self._row_entry(
            cfg, 1, "Max vibration level (0–20)", self.var_sensor_max_level,
            hint="Maps sensor 1.0 → this Lovense Vibrate level",
        )
        self._row_entry(
            cfg, 2, "Command rate (Hz)", self.var_sensor_cmd_hz,
            hint="How often to push intensity while controlling (not 200 Hz)",
        )
        self._row_entry(
            cfg, 3, "Input deadband", self.var_sensor_deadband,
            hint="Values at or below this map to Stop / level 0",
        )
        self._row_entry(
            cfg, 4, "Command timeSec", self.var_sensor_time_sec,
            hint="Duration sent with each Function (stopPrevious=1)",
        )
        ttk.Checkbutton(
            cfg,
            text="Test mode (map + log only, no Lovense emit)",
            variable=self.var_sensor_test,
        ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))

        status = ttk.LabelFrame(parent, text="Live status", padding=12)
        status.pack(fill=tk.X, pady=(0, 8))

        self.var_sensor_stream = tk.StringVar(value="UDP: stopped")
        self.var_sensor_lovense = tk.StringVar(value="Lovense: not connected")
        self.var_sensor_control = tk.StringVar(value="Control: idle")
        self.var_sensor_value = tk.StringVar(value="Value: —")
        self.var_sensor_level = tk.StringVar(value="Level: —")
        self.var_sensor_stats = tk.StringVar(
            value="recv=0  rate=—  gaps=0  cmds=0"
        )

        for var in (
            self.var_sensor_stream,
            self.var_sensor_lovense,
            self.var_sensor_control,
            self.var_sensor_value,
            self.var_sensor_level,
        ):
            ttk.Label(status, textvariable=var, style="Card.TLabel").pack(anchor=tk.W)
        ttk.Label(
            status, textvariable=self.var_sensor_stats, style="Card.TLabel"
        ).pack(anchor=tk.W, pady=(4, 0))

        # Simple value bar (Canvas)
        bar_frame = ttk.Frame(status, style="Card.TFrame")
        bar_frame.pack(fill=tk.X, pady=(8, 0))
        self._sensor_bar = tk.Canvas(
            bar_frame, height=18, bg="#0c0e13", highlightthickness=0
        )
        self._sensor_bar.pack(fill=tk.X)
        self._sensor_bar_rect = self._sensor_bar.create_rectangle(
            0, 0, 0, 18, fill="#ffc14d", width=0
        )

        controls = ttk.Frame(parent)
        controls.pack(fill=tk.X, pady=(0, 8))
        self.btn_sensor_start = ttk.Button(
            controls, text="Start stream + Lovense", command=self.on_sensor_start
        )
        self.btn_sensor_start.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_sensor_begin = ttk.Button(
            controls,
            text="Begin control",
            command=self.on_sensor_begin_control,
            state=tk.DISABLED,
        )
        self.btn_sensor_begin.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_sensor_end = ttk.Button(
            controls,
            text="End control",
            command=self.on_sensor_end_control,
            state=tk.DISABLED,
        )
        self.btn_sensor_end.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_sensor_stop = ttk.Button(
            controls,
            text="Stop stream",
            command=self.on_sensor_stop,
            state=tk.DISABLED,
        )
        self.btn_sensor_stop.pack(side=tk.LEFT)

        rec = ttk.LabelFrame(parent, text="Record / replay", padding=12)
        rec.pack(fill=tk.X, pady=(0, 8))

        self.var_record_path = tk.StringVar(
            value=env("SENSOR_RECORD_PATH", "sensor_capture.csv")
            or "sensor_capture.csv"
        )
        self.var_replay_path = tk.StringVar(
            value=env("SENSOR_REPLAY_PATH", "sensor_capture.csv")
            or "sensor_capture.csv"
        )
        self.var_replay_speed = tk.StringVar(
            value=env("SENSOR_REPLAY_SPEED", "1.0") or "1.0"
        )
        self.var_replay_loop = tk.BooleanVar(value=False)
        self.var_record_status = tk.StringVar(value="Recording: off")

        self._row_entry(
            rec, 0, "Record to CSV", self.var_record_path,
            hint="Live UDP samples only (same columns as tools/udp_receiver.py)",
        )
        ttk.Button(rec, text="Browse…", command=self.on_browse_record_path).grid(
            row=0, column=2, padx=(8, 0), sticky=tk.N, pady=4
        )

        rec_btns = ttk.Frame(rec, style="Card.TFrame")
        rec_btns.grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(4, 8))
        self.btn_record_start = ttk.Button(
            rec_btns, text="Start recording", command=self.on_record_start
        )
        self.btn_record_start.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_record_stop = ttk.Button(
            rec_btns,
            text="Stop recording",
            command=self.on_record_stop,
            state=tk.DISABLED,
        )
        self.btn_record_stop.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(
            rec_btns, textvariable=self.var_record_status, style="Card.TLabel"
        ).pack(side=tk.LEFT)

        self._row_entry(
            rec, 2, "Replay from CSV", self.var_replay_path,
            hint="Plays timed samples without binding UDP / needing the ESP32",
        )
        ttk.Button(rec, text="Browse…", command=self.on_browse_replay_path).grid(
            row=2, column=2, padx=(8, 0), sticky=tk.N, pady=4
        )
        self._row_entry(
            rec, 3, "Replay speed", self.var_replay_speed,
            hint="1.0 = real-time, 2.0 = twice as fast",
        )
        ttk.Checkbutton(
            rec,
            text="Loop replay",
            variable=self.var_replay_loop,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))

        replay_btns = ttk.Frame(rec, style="Card.TFrame")
        replay_btns.grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        self.btn_replay_start = ttk.Button(
            replay_btns, text="Play recording", command=self.on_replay_start
        )
        self.btn_replay_start.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_replay_stop = ttk.Button(
            replay_btns,
            text="Stop replay",
            command=self.on_replay_stop,
            state=tk.DISABLED,
        )
        self.btn_replay_stop.pack(side=tk.LEFT)

        log_box = ttk.LabelFrame(parent, text="Sensor activity", padding=8)
        log_box.pack(fill=tk.BOTH, expand=True)
        self.txt_sensor_log = tk.Text(
            log_box,
            height=10,
            wrap=tk.WORD,
            bg="#0c0e13",
            fg="#c5d0e0",
            insertbackground="#e8ecf4",
            relief=tk.FLAT,
            font=("Consolas", 9),
        )
        slog = ttk.Scrollbar(log_box, command=self.txt_sensor_log.yview)
        self.txt_sensor_log.configure(yscrollcommand=slog.set)
        self.txt_sensor_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        slog.pack(side=tk.RIGHT, fill=tk.Y)

    def _row_entry(
        self,
        parent: ttk.LabelFrame | ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        show: str | None = None,
        hint: str | None = None,
    ) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky=tk.W, pady=4, padx=(0, 12)
        )
        # Match LabelFrame card surface so labels/entries don't sit on a mismatched strip.
        cell_style = (
            "Card.TFrame" if isinstance(parent, ttk.LabelFrame) else "TFrame"
        )
        cell = ttk.Frame(parent, style=cell_style)
        cell.grid(row=row, column=1, sticky=tk.EW, pady=4)
        parent.columnconfigure(1, weight=1)
        ttk.Entry(cell, textvariable=variable, show=show or "").pack(fill=tk.X)
        if hint:
            ttk.Label(cell, text=hint, style="Muted.TLabel").pack(anchor=tk.W)

    def _row_readonly(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky=tk.W, pady=3, padx=(0, 12)
        )
        ttk.Label(parent, textvariable=variable, style="Card.TLabel").grid(
            row=row, column=1, sticky=tk.W, pady=3
        )

    # --- env / readiness ----------------------------------------------------

    def _load_from_env(self) -> None:
        load_dotenv(ENV_PATH)
        self.var_server.set(env("PAIRING_SERVER_URL") or "")
        self.var_api_key.set(env("CONTROLLER_API_KEY") or "")
        self.var_token.set(env("LOVENSE_TOKEN") or "")
        self.var_model.set(env("VOSK_MODEL") or "")
        # Prefill identity labels from local env if present
        if env("LOVENSE_UID"):
            self.var_uid.set(env("LOVENSE_UID") or "—")
        if env("LOVENSE_PLATFORM"):
            self.var_platform.set(env("LOVENSE_PLATFORM") or "—")
        if env("LOVENSE_UNAME"):
            self.var_uname.set(env("LOVENSE_UNAME") or "—")
        self._update_ready()

    def _timeout(self) -> float:
        try:
            return max(1.0, float(self.var_timeout.get().strip() or "10"))
        except ValueError:
            return 10.0

    def _control_ready(self) -> bool:
        paired = self.var_paired.get() == "yes" or bool(
            self._identity and self._identity.get("paired")
        )
        uid = self.var_uid.get().strip()
        platform = self.var_platform.get().strip()
        token = self.var_token.get().strip()
        return bool(
            paired
            and uid
            and uid != "—"
            and platform
            and platform != "—"
            and token
        )

    def _update_ready(self) -> None:
        if self._control_ready():
            self.var_ready.set(
                "Ready for control: yes — identity + developer token available"
            )
        elif self.var_uid.get() not in ("", "—") and not self.var_token.get().strip():
            self.var_ready.set(
                "Ready for control: almost — set local LOVENSE_TOKEN"
            )
        else:
            self.var_ready.set(
                "Ready for control: no — fetch a paired identity and set token"
            )

    def _set_raw(self, data: Any) -> None:
        self.txt_raw.delete("1.0", tk.END)
        try:
            text = json.dumps(data, indent=2, ensure_ascii=False)
        except TypeError:
            text = str(data)
        self.txt_raw.insert(tk.END, text)

    def _apply_identity(self, data: dict[str, Any]) -> None:
        self._identity = data
        paired = bool(data.get("paired"))
        self.var_status.set(str(data.get("status") or data.get("message") or "—"))
        self.var_paired.set("yes" if paired else "no")
        self.var_uid.set(str(data.get("uid") or "—"))
        self.var_platform.set(str(data.get("platform") or "—"))
        self.var_uname.set(str(data.get("uname") or "—"))
        self.var_paired_at.set(str(data.get("paired_at") or "—"))
        self._set_raw(data)
        self._update_ready()

    def _log(self, message: str) -> None:
        self.txt_log.insert(tk.END, message.rstrip() + "\n")
        self.txt_log.see(tk.END)

    def _sensor_log(self, message: str) -> None:
        self.txt_sensor_log.insert(tk.END, message.rstrip() + "\n")
        self.txt_sensor_log.see(tk.END)

    def _queue_ui(self, kind: str, payload: Any = None) -> None:
        self._ui_queue.put((kind, payload))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "sensor_log":
                    self._sensor_log(str(payload))
                elif kind == "status":
                    self.var_listen_state.set(str(payload))
                elif kind == "error":
                    self._log(f"ERROR: {payload}")
                    self.var_listen_state.set("Error")
                elif kind == "sensor_error":
                    self._sensor_log(f"ERROR: {payload}")
                    self.var_sensor_stream.set("UDP: error")
                elif kind == "sensor_replay_finished":
                    self._on_replay_finished_ui()
                elif kind == "listening_stopped":
                    self._set_listening_ui(False)
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    # --- connection handlers ------------------------------------------------

    def on_test(self) -> None:
        url = self.var_server.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Enter the pairing server URL.")
            return
        try:
            health = fetch_pairing_health(
                pairing_server_url=url, timeout=self._timeout()
            )
            self._set_raw(health)
            messagebox.showinfo(
                "Connection OK",
                f"Reached pairing server.\n"
                f"ok={health.get('ok')}  role={health.get('role')}\n"
                f"controller API key on server: {health.get('has_controller_api_key')}",
            )
        except ControllerConfigError as exc:
            messagebox.showerror("Connection failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "Connection failed", f"{exc}\n\n{traceback.format_exc()}"
            )

    def on_fetch(self) -> None:
        url = self.var_server.get().strip()
        key = self.var_api_key.get().strip()
        if not url or not key:
            messagebox.showwarning(
                "Missing settings",
                "Pairing server URL and controller API key are required.",
            )
            return
        try:
            try:
                data = fetch_owner_identity(
                    pairing_server_url=url,
                    api_key=key,
                    timeout=self._timeout(),
                    require_paired=True,
                )
            except ControllerConfigError as exc:
                msg = str(exc).lower()
                if "not finished pairing" in msg or "not return a uid" in msg:
                    data = fetch_owner_identity(
                        pairing_server_url=url,
                        api_key=key,
                        timeout=self._timeout(),
                        require_paired=False,
                    )
                    self._apply_identity(data)
                    messagebox.showwarning(
                        "Not paired yet",
                        f"{exc}\n\nShowing latest status from the server.",
                    )
                    return
                raise

            self._apply_identity(data)
            if data.get("uid"):
                upsert_env_value(ENV_PATH, "LOVENSE_UID", str(data["uid"]))
            if data.get("platform"):
                upsert_env_value(ENV_PATH, "LOVENSE_PLATFORM", str(data["platform"]))
            if data.get("uname"):
                upsert_env_value(ENV_PATH, "LOVENSE_UNAME", str(data["uname"]))
            messagebox.showinfo(
                "Identity loaded",
                f"UID: {data.get('uid')}\n"
                f"Platform: {data.get('platform')}\n"
                f"Paired: {data.get('paired')}\n\n"
                "Saved uid/platform/uname to local .env.",
            )
        except ControllerConfigError as exc:
            messagebox.showerror("Fetch failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Fetch failed", f"{exc}\n\n{traceback.format_exc()}")

    def on_save(self) -> None:
        url = self.var_server.get().strip()
        key = self.var_api_key.get().strip()
        token = self.var_token.get().strip()
        model = self.var_model.get().strip()
        if url:
            upsert_env_value(ENV_PATH, "PAIRING_SERVER_URL", url)
            os.environ["PAIRING_SERVER_URL"] = url
        if key:
            upsert_env_value(ENV_PATH, "CONTROLLER_API_KEY", key)
            os.environ["CONTROLLER_API_KEY"] = key
        if token:
            upsert_env_value(ENV_PATH, "LOVENSE_TOKEN", token)
            os.environ["LOVENSE_TOKEN"] = token
        if model:
            upsert_env_value(ENV_PATH, "VOSK_MODEL", model)
            os.environ["VOSK_MODEL"] = model
        self._update_ready()
        messagebox.showinfo("Saved", f"Settings written to:\n{ENV_PATH}")

    # --- triggers -----------------------------------------------------------

    def _refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, entry in enumerate(self._entries):
            phrases = ", ".join(entry.get("phrases") or [])
            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(
                    phrases,
                    entry.get("action", ""),
                    entry.get("timeSec", ""),
                    entry.get("cooldown", ""),
                    entry.get("toy", "") or "",
                ),
            )

    def _sync_defaults_from_vars(self) -> None:
        try:
            self._defaults = {
                "action": self.var_def_action.get().strip() or DEFAULT_ACTION,
                "timeSec": float(self.var_def_time.get() or DEFAULT_TIME_SEC),
                "cooldown": float(self.var_def_cooldown.get() or DEFAULT_COOLDOWN),
            }
        except ValueError as exc:
            raise TriggerConfigError(
                "Default timeSec/cooldown must be numbers."
            ) from exc

    def _load_triggers_file(self, *, silent: bool = False) -> None:
        path = Path(self.var_triggers_path.get().strip() or self._triggers_path)
        self._triggers_path = path
        if not path.is_file():
            self._entries = []
            self._refresh_tree()
            if not silent:
                messagebox.showwarning(
                    "Not found",
                    f"No triggers file at:\n{path}\n\nAdd triggers and Save to create it.",
                )
            return
        try:
            raw = load_triggers_document(path)
            defaults = raw.get("defaults") or {}
            self._defaults = {
                "action": str(defaults.get("action") or DEFAULT_ACTION),
                "timeSec": float(defaults.get("timeSec", DEFAULT_TIME_SEC)),
                "cooldown": float(defaults.get("cooldown", DEFAULT_COOLDOWN)),
            }
            self.var_def_action.set(str(self._defaults["action"]))
            self.var_def_time.set(str(self._defaults["timeSec"]))
            self.var_def_cooldown.set(str(self._defaults["cooldown"]))
            self._entries = normalize_trigger_entries(raw.get("triggers") or [])
            self._refresh_tree()
            if not silent:
                messagebox.showinfo(
                    "Loaded",
                    f"Loaded {len(self._entries)} trigger group(s) from:\n{path}",
                )
        except TriggerConfigError as exc:
            if not silent:
                messagebox.showerror("Load failed", str(exc))

    def on_browse_triggers(self) -> None:
        path = filedialog.askopenfilename(
            title="Triggers JSON",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
            initialdir=str(self._triggers_path.parent),
        )
        if path:
            self.var_triggers_path.set(path)
            self._load_triggers_file()

    def on_save_triggers(self) -> None:
        try:
            self._sync_defaults_from_vars()
            path = Path(self.var_triggers_path.get().strip() or self._triggers_path)
            save_triggers_document(
                path, defaults=self._defaults, entries=self._entries
            )
            self._triggers_path = path
            upsert_env_value(ENV_PATH, "TRIGGERS_FILE", str(path))
            messagebox.showinfo("Saved", f"Triggers written to:\n{path}")
            # Rebuild engine if listening
            if self._listener and self._listener.running:
                self._rebuild_engine()
                self._log("Reloaded trigger engine from saved config.")
        except TriggerConfigError as exc:
            messagebox.showerror("Save failed", str(exc))
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

    def on_add_trigger(self) -> None:
        dlg = TriggerEditDialog(self, "Add trigger")
        self.wait_window(dlg)
        if dlg.result:
            self._entries.append(dlg.result)
            self._refresh_tree()

    def on_edit_trigger(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Edit", "Select a trigger row first.")
            return
        idx = int(sel[0])
        dlg = TriggerEditDialog(self, "Edit trigger", self._entries[idx])
        self.wait_window(dlg)
        if dlg.result:
            self._entries[idx] = dlg.result
            self._refresh_tree()

    def on_remove_trigger(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        del self._entries[idx]
        self._refresh_tree()

    def on_duplicate_trigger(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        copy = json.loads(json.dumps(self._entries[idx]))
        self._entries.insert(idx + 1, copy)
        self._refresh_tree()

    # --- voice --------------------------------------------------------------

    def _refresh_devices(self) -> None:
        values = ["default"]
        try:
            for _idx, label in list_input_device_choices():
                values.append(label)
        except Exception:  # noqa: BLE001
            pass
        self.cmb_device["values"] = values
        if self.var_device.get() not in values:
            self.var_device.set("default")

    def on_browse_model(self) -> None:
        path = filedialog.askdirectory(title="Select unpacked Vosk model folder")
        if path:
            self.var_model.set(path)

    def _device_arg(self) -> str | int | None:
        raw = self.var_device.get().strip()
        if not raw or raw == "default":
            return None
        if raw[0].isdigit() and ":" in raw:
            return int(raw.split(":", 1)[0].strip())
        return raw

    def _rebuild_engine(self) -> None:
        self._sync_defaults_from_vars()
        defaults = ActionDefaults(
            action=str(self._defaults["action"]),
            time_sec=float(self._defaults["timeSec"]),
            cooldown=float(self._defaults["cooldown"]),
        )
        actions = expand_entries_to_actions(self._entries, defaults)
        if not actions:
            raise TriggerConfigError("No triggers configured. Add at least one phrase.")

        def on_match(trigger: TriggerAction, text: str) -> None:
            self._queue_ui(
                "log",
                f'MATCH "{trigger.phrase}" in {text!r} → '
                f"{trigger.action} / {trigger.time_sec}s",
            )
            client = self._client
            test_mode = self.var_test_mode.get()
            if test_mode or client is None:
                if test_mode:
                    self._queue_ui(
                        "log",
                        f"  [test] would emit Function action={trigger.action!r} "
                        f"timeSec={trigger.time_sec}",
                    )
                return
            try:
                client.send_trigger(trigger, test_mode=False)
                self._queue_ui("log", f"  → sent {trigger.action} ({trigger.time_sec}s)")
            except Exception as exc:  # noqa: BLE001
                self._queue_ui("error", f"Send failed: {exc}")

        self._engine = TriggerEngine(
            actions, on_match, log_matches=False
        )

    def _ensure_lovense_client(self, *, force: bool = False) -> LovenseSocketClient | None:
        # Voice tab test mode skips connect unless force=True (sensor path).
        if not force and self.var_test_mode.get():
            return None
        if not self._control_ready():
            raise LovenseError(
                "Not ready: fetch paired identity and set developer token first."
            )
        uid = self.var_uid.get().strip()
        platform = self.var_platform.get().strip()
        uname = self.var_uname.get().strip()
        if uname in ("", "—"):
            uname = DEFAULT_UNAME
        token = self.var_token.get().strip()
        if self._client is not None:
            # Rebuild if identity changed
            if (
                self._client.uid == uid
                and self._client.platform == platform
                and self._client.token == token
            ):
                return self._client
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

        client = LovenseSocketClient(
            token=token,
            uid=uid,
            uname=uname,
            platform=platform,
            timeout=self._timeout(),
        )
        client.connect()
        self._client = client
        return client

    def _set_listening_ui(self, listening: bool) -> None:
        self.btn_start.configure(state=tk.DISABLED if listening else tk.NORMAL)
        self.btn_stop.configure(state=tk.NORMAL if listening else tk.DISABLED)
        if not listening:
            self.var_listen_state.set("Stopped")

    def on_start_listening(self) -> None:
        if self._listener and self._listener.running:
            return
        model = self.var_model.get().strip()
        if not model:
            messagebox.showwarning(
                "Model required",
                "Set the path to an unpacked Vosk model directory.",
            )
            return
        if not Path(model).is_dir():
            messagebox.showwarning(
                "Model not found",
                f"Model path is not a directory:\n{model}",
            )
            return
        try:
            self._rebuild_engine()
        except TriggerConfigError as exc:
            messagebox.showerror("Triggers", str(exc))
            return

        if not self.var_test_mode.get():
            try:
                self._ensure_lovense_client()
                self._log("Lovense Socket connected.")
            except LovenseError as exc:
                messagebox.showerror("Lovense", str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    "Lovense", f"{exc}\n\n{traceback.format_exc()}"
                )
                return
        else:
            self._log("Test mode: will not connect to Lovense.")

        engine = self._engine
        assert engine is not None

        def on_text(text: str) -> None:
            engine.handle_text(text)

        def on_status(msg: str) -> None:
            self._queue_ui("log", msg)
            if msg.startswith("Listening"):
                self._queue_ui("status", "Listening")

        def on_error(msg: str) -> None:
            self._queue_ui("error", msg)
            self._queue_ui("listening_stopped", None)

        try:
            self._listener = VoiceListener(
                model_path=model,
                on_text=on_text,
                device=self._device_arg(),
                verbose=self.var_verbose.get(),
                on_status=on_status,
                on_error=on_error,
            )
            self._listener.start()
        except RecognitionError as exc:
            messagebox.showerror("Voice", str(exc))
            return

        self._set_listening_ui(True)
        self.var_listen_state.set("Starting…")
        self._log(
            f"Voice listening started with {len(engine.triggers)} phrase trigger(s)."
        )

    def on_stop_listening(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None
        self._set_listening_ui(False)
        self._log("Stop requested.")

    # --- sensor (UDP motion → Lovense) --------------------------------------

    def _on_notebook_tab_changed(self, _event: object | None = None) -> None:
        nb = self._notebook
        if nb is None:
            return
        try:
            title = nb.tab(nb.select(), "text")
        except tk.TclError:
            return
        if title == "Sensor":
            # Do not steal the stream if a CSV replay is already running.
            if isinstance(self._sensor_receiver, SensorReplay):
                return
            # Selecting the Sensor tab starts UDP + Lovense (idempotent).
            self.after(50, lambda: self.on_sensor_start(from_tab=True))

    def _sensor_is_live_udp(self) -> bool:
        return isinstance(self._sensor_receiver, UdpSensorReceiver) and bool(
            self._sensor_receiver.running
        )

    def _sensor_is_replay(self) -> bool:
        return isinstance(self._sensor_receiver, SensorReplay) and bool(
            self._sensor_receiver.running
        )

    def _sensor_parse_port(self) -> int:
        try:
            port = int(self.var_udp_port.get().strip() or DEFAULT_UDP_PORT)
        except ValueError as exc:
            raise ValueError("UDP port must be an integer.") from exc
        if not (1 <= port <= 65535):
            raise ValueError("UDP port must be between 1 and 65535.")
        return port

    def _sensor_mapping_params(self) -> dict[str, float | int]:
        try:
            max_level = int(float(self.var_sensor_max_level.get().strip() or "20"))
            cmd_hz = float(self.var_sensor_cmd_hz.get().strip() or "10")
            deadband = float(self.var_sensor_deadband.get().strip() or "0")
            time_sec = float(self.var_sensor_time_sec.get().strip() or "1")
        except ValueError as exc:
            raise ValueError(
                "Max level, command rate, deadband, and timeSec must be numbers."
            ) from exc
        max_level = max(0, min(20, max_level))
        if cmd_hz <= 0:
            cmd_hz = 10.0
        if cmd_hz > 30:
            cmd_hz = 30.0  # keep socket traffic reasonable
        deadband = max(0.0, min(0.99, deadband))
        if time_sec < 0:
            time_sec = 0.0
        return {
            "max_level": max_level,
            "cmd_hz": cmd_hz,
            "deadband": deadband,
            "time_sec": time_sec,
        }

    def _set_sensor_stream_ui(self, running: bool) -> None:
        replaying = self._sensor_is_replay()
        live_udp = self._sensor_is_live_udp()
        recording = self._sensor_recorder is not None and not self._sensor_recorder.closed

        self.btn_sensor_start.configure(
            state=tk.DISABLED if running else tk.NORMAL
        )
        self.btn_sensor_stop.configure(
            state=tk.NORMAL if running else tk.DISABLED
        )
        self.btn_sensor_begin.configure(
            state=tk.NORMAL if running and not self._sensor_control_active else tk.DISABLED
        )
        self.btn_sensor_end.configure(
            state=tk.NORMAL if self._sensor_control_active else tk.DISABLED
        )
        # Record only while live UDP is up; replay is exclusive of live stream.
        self.btn_record_start.configure(
            state=tk.NORMAL if live_udp and not recording else tk.DISABLED
        )
        self.btn_record_stop.configure(
            state=tk.NORMAL if recording else tk.DISABLED
        )
        self.btn_replay_start.configure(
            state=tk.DISABLED if running else tk.NORMAL
        )
        self.btn_replay_stop.configure(
            state=tk.NORMAL if replaying else tk.DISABLED
        )

    def _stop_sensor_recorder(self, *, log: bool = True) -> None:
        rec = self._sensor_recorder
        self._sensor_recorder = None
        if rec is None:
            self.var_record_status.set("Recording: off")
            return
        try:
            count = rec.close()
        except Exception as exc:  # noqa: BLE001
            if log:
                self._sensor_log(f"Recorder close failed: {exc}")
            count = rec.count
        self.var_record_status.set("Recording: off")
        if log:
            self._sensor_log(
                f"Recording stopped → {rec.path} ({count} sample(s))."
            )

    def on_sensor_start(self, *, from_tab: bool = False) -> None:
        """Start UDP receiver and connect Lovense Socket API."""
        if isinstance(self._sensor_receiver, SensorReplay) and self._sensor_receiver.running:
            if from_tab:
                return
            if not messagebox.askyesno(
                "Sensor",
                "A CSV replay is running. Stop it and start the live UDP stream?",
            ):
                return
            self.on_replay_stop()

        if self._sensor_receiver is not None and self._sensor_receiver.running:
            if not from_tab:
                self._sensor_log("UDP stream already running.")
            # Still ensure Lovense if identity became ready later
            self._sensor_try_connect_lovense(quiet=from_tab)
            return

        try:
            port = self._sensor_parse_port()
        except ValueError as exc:
            if not from_tab:
                messagebox.showwarning("UDP port", str(exc))
            else:
                self._sensor_log(f"Cannot start stream: {exc}")
            return

        def on_error(msg: str) -> None:
            self._queue_ui("sensor_error", msg)

        def on_sample(packet: Any) -> None:
            rec = self._sensor_recorder
            if rec is not None and not rec.closed:
                try:
                    rec.record(packet)
                except Exception:  # noqa: BLE001
                    pass

        try:
            receiver = UdpSensorReceiver(
                host="0.0.0.0",
                port=port,
                on_sample=on_sample,
                on_error=on_error,
            )
            receiver.start()
        except OSError as exc:
            msg = f"Could not bind UDP {port}: {exc}"
            if not from_tab:
                messagebox.showerror("UDP", msg)
            else:
                self._sensor_log(msg)
            return
        except Exception as exc:  # noqa: BLE001
            if not from_tab:
                messagebox.showerror(
                    "UDP", f"{exc}\n\n{traceback.format_exc()}"
                )
            else:
                self._sensor_log(f"UDP start failed: {exc}")
            return

        self._sensor_receiver = receiver
        self.var_sensor_stream.set(f"UDP: listening on 0.0.0.0:{port}")
        self._set_sensor_stream_ui(True)
        self._sensor_log(f"UDP receiver started on port {port}.")
        upsert_env_value(ENV_PATH, "UDP_SENSOR_PORT", str(port))

        self._sensor_try_connect_lovense(quiet=from_tab)

    def _sensor_try_connect_lovense(self, *, quiet: bool = False) -> bool:
        if self.var_sensor_test.get():
            self.var_sensor_lovense.set("Lovense: test mode (no emit)")
            if not quiet:
                self._sensor_log("Test mode: Lovense socket not required.")
            return True
        if not self._control_ready():
            self.var_sensor_lovense.set(
                "Lovense: not ready — fetch identity + set token on Connection tab"
            )
            if not quiet:
                self._sensor_log(
                    "Lovense not connected yet (need paired identity + token)."
                )
            return False
        try:
            self._ensure_lovense_client(force=True)
            self.var_sensor_lovense.set("Lovense: Socket API connected")
            self._sensor_log("Lovense Socket API connected.")
            return True
        except LovenseError as exc:
            self.var_sensor_lovense.set(f"Lovense: error — {exc}")
            if not quiet:
                messagebox.showerror("Lovense", str(exc))
            else:
                self._sensor_log(f"Lovense connect failed: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            self.var_sensor_lovense.set(f"Lovense: error — {exc}")
            if not quiet:
                messagebox.showerror(
                    "Lovense", f"{exc}\n\n{traceback.format_exc()}"
                )
            else:
                self._sensor_log(f"Lovense connect failed: {exc}")
            return False

    def on_sensor_stop(self) -> None:
        if self._sensor_control_active:
            self.on_sensor_end_control()
        self._stop_sensor_recorder(log=True)
        was_replay = isinstance(self._sensor_receiver, SensorReplay)
        if self._sensor_receiver is not None:
            try:
                self._sensor_receiver.stop()
            except Exception:  # noqa: BLE001
                pass
            self._sensor_receiver = None
        self.var_sensor_stream.set("Replay: stopped" if was_replay else "UDP: stopped")
        self.var_sensor_control.set("Control: idle")
        self.var_sensor_value.set("Value: —")
        self.var_sensor_level.set("Level: —")
        self._set_sensor_stream_ui(False)
        self._sensor_log("Replay stopped." if was_replay else "UDP stream stopped.")

    def on_browse_record_path(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Record sensor CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile=Path(self.var_record_path.get() or "sensor_capture.csv").name,
        )
        if path:
            self.var_record_path.set(path)

    def on_browse_replay_path(self) -> None:
        path = filedialog.askopenfilename(
            title="Open sensor recording",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.var_replay_path.set(path)

    def on_record_start(self) -> None:
        if not self._sensor_is_live_udp():
            messagebox.showwarning(
                "Record",
                "Start the live UDP stream first, then start recording.",
            )
            return
        if self._sensor_recorder is not None and not self._sensor_recorder.closed:
            self._sensor_log("Already recording.")
            return
        path_text = (self.var_record_path.get() or "").strip()
        if not path_text:
            messagebox.showwarning("Record", "Choose a CSV path for the recording.")
            return
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            self._sensor_recorder = SensorRecorder(path)
        except OSError as exc:
            messagebox.showerror("Record", f"Could not open {path}: {exc}")
            return
        self.var_record_path.set(str(path))
        upsert_env_value(ENV_PATH, "SENSOR_RECORD_PATH", str(path))
        self.var_record_status.set(f"Recording: {path.name} (0)")
        self._set_sensor_stream_ui(True)
        self._sensor_log(f"Recording live UDP → {path}")

    def on_record_stop(self) -> None:
        if self._sensor_recorder is None:
            return
        self._stop_sensor_recorder(log=True)
        self._set_sensor_stream_ui(
            self._sensor_receiver is not None and self._sensor_receiver.running
        )

    def on_replay_start(self) -> None:
        if self._sensor_receiver is not None and self._sensor_receiver.running:
            messagebox.showwarning(
                "Replay",
                "Stop the current stream (UDP or replay) before playing a recording.",
            )
            return
        path_text = (self.var_replay_path.get() or "").strip()
        if not path_text:
            messagebox.showwarning("Replay", "Choose a CSV recording to play.")
            return
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            speed = float(self.var_replay_speed.get().strip() or "1.0")
        except ValueError:
            messagebox.showwarning("Replay", "Replay speed must be a number.")
            return
        if speed <= 0:
            messagebox.showwarning("Replay", "Replay speed must be > 0.")
            return

        try:
            samples = load_recording_csv(path)
        except RecordingError as exc:
            messagebox.showerror("Replay", str(exc))
            return

        def on_error(msg: str) -> None:
            self._queue_ui("sensor_error", msg)

        def on_finished() -> None:
            self._queue_ui("sensor_replay_finished")

        try:
            player = SensorReplay(
                samples,
                loop=bool(self.var_replay_loop.get()),
                speed=speed,
                on_error=on_error,
                on_finished=on_finished,
                label=path.name,
            )
            player.start()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "Replay", f"{exc}\n\n{traceback.format_exc()}"
            )
            return

        self._sensor_receiver = player
        self.var_replay_path.set(str(path))
        upsert_env_value(ENV_PATH, "SENSOR_REPLAY_PATH", str(path))
        upsert_env_value(ENV_PATH, "SENSOR_REPLAY_SPEED", str(speed))
        dur = player.duration_sec
        loop_txt = ", loop" if self.var_replay_loop.get() else ""
        self.var_sensor_stream.set(
            f"Replay: {path.name}  ({player.sample_count} samples, "
            f"{dur:.1f}s @ {speed:g}×{loop_txt})"
        )
        self._set_sensor_stream_ui(True)
        self._sensor_log(
            f"Replay started: {path} ({player.sample_count} samples, "
            f"{dur:.2f}s timeline, speed={speed:g}×"
            f"{', loop' if self.var_replay_loop.get() else ''})."
        )
        self._sensor_try_connect_lovense(quiet=True)

    def on_replay_stop(self) -> None:
        if not isinstance(self._sensor_receiver, SensorReplay):
            return
        if self._sensor_control_active:
            self.on_sensor_end_control()
        try:
            self._sensor_receiver.stop()
        except Exception:  # noqa: BLE001
            pass
        self._sensor_receiver = None
        self.var_sensor_stream.set("Replay: stopped")
        self.var_sensor_value.set("Value: —")
        self.var_sensor_level.set("Level: —")
        self._set_sensor_stream_ui(False)
        self._sensor_log("Replay stopped.")

    def _on_replay_finished_ui(self) -> None:
        """Called on the UI thread when a non-looping replay ends."""
        if not isinstance(self._sensor_receiver, SensorReplay):
            return
        if self._sensor_control_active:
            self.on_sensor_end_control()
        # Thread already exited; drop reference.
        loops = self._sensor_receiver.loops_completed
        recv = self._sensor_receiver.stats.received
        self._sensor_receiver = None
        self.var_sensor_stream.set("Replay: finished")
        self._set_sensor_stream_ui(False)
        self._sensor_log(
            f"Replay finished (played {recv} sample(s), loops={loops})."
        )

    def on_sensor_begin_control(self) -> None:
        if self._sensor_receiver is None or not self._sensor_receiver.running:
            messagebox.showwarning(
                "Sensor",
                "Start the UDP stream or play a recording first, then "
                "Begin control.",
            )
            return
        try:
            self._sensor_mapping_params()
        except ValueError as exc:
            messagebox.showwarning("Mapping", str(exc))
            return

        if not self.var_sensor_test.get():
            if not self._sensor_try_connect_lovense(quiet=False):
                return
        else:
            self.var_sensor_lovense.set("Lovense: test mode (no emit)")

        self._sensor_control_active = True
        self._sensor_last_level = None
        self._sensor_last_send_mono = 0.0
        self._sensor_commands_sent = 0
        source = (
            "replay" if isinstance(self._sensor_receiver, SensorReplay) else "UDP"
        )
        self.var_sensor_control.set(
            f"Control: ACTIVE — mapping {source} → toy"
        )
        self._set_sensor_stream_ui(True)
        self._sensor_log(
            f"Begin control ({source}): sensor values mapped to Vibrate:0–20 "
            f"(cmd rate ≤ {self.var_sensor_cmd_hz.get()} Hz)."
        )

    def on_sensor_end_control(self) -> None:
        was_active = self._sensor_control_active
        self._sensor_control_active = False
        self.var_sensor_control.set("Control: idle")
        running = (
            self._sensor_receiver is not None and self._sensor_receiver.running
        )
        self._set_sensor_stream_ui(running)
        if was_active:
            # Stop toy when leaving continuous control
            if not self.var_sensor_test.get() and self._client is not None:
                try:
                    self._client.send_intensity(0, time_sec=0.0, stop_previous=1)
                    self._sensor_log("Sent Stop to Lovense.")
                except Exception as exc:  # noqa: BLE001
                    self._sensor_log(f"Stop command failed: {exc}")
            else:
                self._sensor_log("End control (test mode — no Stop emit).")
            self._sensor_log(
                f"Control ended. Commands sent this session: {self._sensor_commands_sent}"
            )

    def _sensor_update_bar(self, value: float | None) -> None:
        canvas = self._sensor_bar
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 1)
        height = 18
        if value is None:
            canvas.coords(self._sensor_bar_rect, 0, 0, 0, height)
            return
        v = max(0.0, min(1.0, float(value)))
        canvas.coords(self._sensor_bar_rect, 0, 0, int(width * v), height)

    def _sensor_tick(self) -> None:
        """UI + control loop: poll latest sample and optionally emit intensity."""
        try:
            receiver = self._sensor_receiver
            # Keep recording counter fresh even between packets.
            rec = self._sensor_recorder
            if rec is not None and not rec.closed:
                self.var_record_status.set(
                    f"Recording: {rec.path.name} ({rec.count})"
                )

            if receiver is not None and receiver.running:
                value = receiver.latest_value
                stats = receiver.stats
                if value is not None:
                    self.var_sensor_value.set(f"Value: {value:.4f}")
                    self._sensor_update_bar(value)
                else:
                    waiting = (
                        "Value: (replay starting…)"
                        if isinstance(receiver, SensorReplay)
                        else "Value: (waiting for packets…)"
                    )
                    self.var_sensor_value.set(waiting)
                    self._sensor_update_bar(None)

                rate = stats.rate_hz(time.monotonic())
                extra = ""
                if isinstance(receiver, SensorReplay):
                    extra = f"  loops={receiver.loops_completed}"
                self.var_sensor_stats.set(
                    f"recv={stats.received}  rate≈{rate:.1f} Hz  "
                    f"gaps={stats.gaps}  reject={stats.rejected}  "
                    f"cmds={self._sensor_commands_sent}{extra}"
                )

                if value is not None:
                    try:
                        params = self._sensor_mapping_params()
                    except ValueError:
                        params = {
                            "max_level": 20,
                            "cmd_hz": 10.0,
                            "deadband": 0.02,
                            "time_sec": 1.0,
                        }
                    level = map_value_to_level(
                        value,
                        max_level=int(params["max_level"]),
                        deadband=float(params["deadband"]),
                    )
                    self.var_sensor_level.set(
                        f"Level: {level} / {int(params['max_level'])} "
                        f"({'Stop' if level == 0 else f'Vibrate:{level}'})"
                    )

                    if self._sensor_control_active:
                        self._sensor_maybe_send(
                            level,
                            time_sec=float(params["time_sec"]),
                            cmd_hz=float(params["cmd_hz"]),
                        )
            elif (
                receiver is not None
                and isinstance(receiver, SensorReplay)
                and not receiver.running
            ):
                # Non-loop replay ended between ticks; clean up if needed.
                self._on_replay_finished_ui()
            elif self._sensor_receiver is None:
                # keep idle labels stable
                pass
        except Exception:  # noqa: BLE001
            # Never let the tick crash the GUI event loop
            pass
        self.after(50, self._sensor_tick)

    def _sensor_maybe_send(
        self, level: int, *, time_sec: float, cmd_hz: float
    ) -> None:
        now = time.monotonic()
        min_interval = 1.0 / max(cmd_hz, 0.1)
        level_changed = self._sensor_last_level is None or level != self._sensor_last_level
        due = (now - self._sensor_last_send_mono) >= min_interval
        # Always send on level change if enough time passed; also refresh
        # while non-zero so the toy doesn't time out mid-stroke.
        if not due:
            return
        if not level_changed and level == 0 and self._sensor_last_level == 0:
            return
        if not level_changed and level > 0 and (now - self._sensor_last_send_mono) < max(
            min_interval, time_sec * 0.5
        ):
            # Allow periodic refresh at cmd_hz even if level holds
            pass

        test_mode = self.var_sensor_test.get()
        try:
            if test_mode:
                # Log sparingly: only on level changes
                if level_changed:
                    self._sensor_log(
                        f"[test] map → {'Stop' if level == 0 else f'Vibrate:{level}'}"
                    )
            else:
                client = self._client
                if client is None:
                    if not self._sensor_try_connect_lovense(quiet=True):
                        return
                    client = self._client
                if client is None:
                    return
                client.send_intensity(
                    level,
                    time_sec=time_sec if level > 0 else 0.0,
                    stop_previous=1,
                    test_mode=False,
                )
                if level_changed:
                    self._sensor_log(
                        f"→ {'Stop' if level == 0 else f'Vibrate:{level}'}"
                    )
            self._sensor_last_level = level
            self._sensor_last_send_mono = now
            self._sensor_commands_sent += 1
        except Exception as exc:  # noqa: BLE001
            self._sensor_log(f"Send failed: {exc}")

    def on_close(self) -> None:
        try:
            if self._sensor_control_active:
                self.on_sensor_end_control()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._stop_sensor_recorder(log=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._sensor_receiver is not None:
                self._sensor_receiver.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._listener:
                self._listener.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._client:
                self._client.close()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def main() -> None:
    load_dotenv(ENV_PATH)
    app = ControllerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
