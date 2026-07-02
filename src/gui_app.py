from __future__ import annotations

import asyncio
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Callable

from .bot import NaraBotRunner
from .channel_picker import fetch_discord_snapshot
from .config import ConfigError, load_config, update_env_values
from .logging_utils import setup_logging
from .utils import ensure_dir, write_json


def run_nara_gui(project_root: Path) -> int:
    app = NaraControlApp(project_root)
    app.mainloop()
    return 0


class GuiLogHandler(logging.Handler):
    def __init__(self, target_queue: queue.Queue[tuple[str, Any]]) -> None:
        super().__init__()
        self.target_queue = target_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.target_queue.put(("log", self.format(record)))


class BotWorker:
    def __init__(self, config, logger: logging.Logger, target_queue: queue.Queue[tuple[str, Any]]) -> None:
        self.config = config
        self.logger = logger
        self.target_queue = target_queue
        self.loop: asyncio.AbstractEventLoop | None = None
        self.runner: NaraBotRunner | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()

    @property
    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._thread_main, name="nara-bot", daemon=True)
        self.thread.start()

    def submit(self, label: str, factory: Callable[[NaraBotRunner], Any]) -> None:
        if self.loop is None or self.runner is None or not self.is_alive:
            self.target_queue.put(("log", "Bot is not running yet."))
            return

        async def run_command() -> Any:
            result = factory(self.runner)
            if asyncio.iscoroutine(result):
                return await result
            return result

        future = asyncio.run_coroutine_threadsafe(run_command(), self.loop)
        future.add_done_callback(lambda item: self.target_queue.put(("command_done", (label, item))))

    def stop(self) -> None:
        if self.loop is None or self.runner is None:
            return
        self.submit("Shutdown", lambda runner: runner.shutdown())

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._run_bot())
        except Exception as exc:
            self.target_queue.put(("log", f"Bot stopped because of an error: {exc}"))
        finally:
            self.target_queue.put(("bot_stopped", None))
            self.ready.set()
            if self.loop is not None:
                self.loop.close()

    async def _run_bot(self) -> None:
        self.runner = NaraBotRunner(self.config, self.logger, ui_callback=self._bot_event)
        self.ready.set()
        await self.runner.run()

    def _bot_event(self, kind: str, payload: dict[str, Any]) -> None:
        self.target_queue.put(("bot_event", (kind, payload)))


class NaraControlApp(tk.Tk):
    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root
        self.snapshot: list[dict[str, Any]] = []
        self.guild_by_label: dict[str, dict[str, Any]] = {}
        self.voice_by_label: dict[str, dict[str, Any]] = {}
        self.text_by_label: dict[str, dict[str, Any]] = {}
        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.bot_worker: BotWorker | None = None
        self.is_loading = False
        self.is_closing = False
        self.poll_after_id: str | None = None
        self.last_selection = self._load_last_selection()

        try:
            config = load_config(require_secrets=False, project_root=project_root)
            token = config.discord_token
            gemini_key = config.gemini_api_key
        except Exception:
            token = ""
            gemini_key = ""

        self.title("Nara Meeting Control")
        self.geometry("1060x740")
        self.minsize(900, 640)
        self.configure(bg="#1c1c1e")
        self._build_style()
        self._build_ui(token, gemini_key)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.poll_after_id = self.after(100, self._poll_queue)
        if token:
            self.after(250, self._connect)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#1c1c1e")
        style.configure("Panel.TFrame", background="#2c2c2e")
        style.configure("TLabel", background="#1c1c1e", foreground="#f5f5f7", font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background="#2c2c2e", foreground="#f5f5f7", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#1c1c1e", foreground="#f5f5f7", font=("Segoe UI", 16, "bold"))
        style.configure("Status.TLabel", background="#2c2c2e", foreground="#f5f5f7", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#1c1c1e", foreground="#a1a1a6", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7), background="#3a3a3c", foreground="#f5f5f7")
        style.map("TButton", background=[("active", "#48484a"), ("disabled", "#2c2c2e")], foreground=[("disabled", "#6e6e73")])
        style.configure("Accent.TButton", background="#0a84ff", foreground="#ffffff")
        style.configure("Danger.TButton", background="#b3261e", foreground="#ffffff")
        style.configure("TEntry", fieldbackground="#3a3a3c", foreground="#f5f5f7", insertcolor="#f5f5f7")
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#3a3a3c",
            background="#3a3a3c",
            foreground="#f5f5f7",
            selectbackground="#3a3a3c",
            selectforeground="#f5f5f7",
            arrowcolor="#f5f5f7",
            bordercolor="#5a5a5c",
            lightcolor="#5a5a5c",
            darkcolor="#1c1c1e",
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[
                ("readonly", "#3a3a3c"),
                ("disabled", "#2c2c2e"),
                ("!disabled", "#3a3a3c"),
            ],
            foreground=[
                ("readonly", "#f5f5f7"),
                ("disabled", "#8e8e93"),
                ("!disabled", "#f5f5f7"),
            ],
            selectbackground=[
                ("readonly", "#3a3a3c"),
                ("!disabled", "#3a3a3c"),
            ],
            selectforeground=[
                ("readonly", "#f5f5f7"),
                ("!disabled", "#f5f5f7"),
            ],
            background=[
                ("active", "#48484a"),
                ("readonly", "#3a3a3c"),
                ("disabled", "#2c2c2e"),
            ],
            arrowcolor=[
                ("disabled", "#8e8e93"),
                ("!disabled", "#f5f5f7"),
            ],
        )
        self.option_add("*TCombobox*Listbox.background", "#242426")
        self.option_add("*TCombobox*Listbox.foreground", "#f5f5f7")
        self.option_add("*TCombobox*Listbox.selectBackground", "#0a84ff")
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _build_ui(self, token: str, gemini_key: str) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=18)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)

        ttk.Label(root, text="Nara Meeting Control", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        setup = ttk.Frame(root, style="Panel.TFrame", padding=14)
        setup.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        setup.columnconfigure(1, weight=1)
        setup.columnconfigure(3, weight=1)

        self.token_var = tk.StringVar(value=token)
        self.gemini_var = tk.StringVar(value=gemini_key)
        ttk.Label(setup, text="Discord token", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(setup, textvariable=self.token_var, show="*", width=48).grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(setup, text="Gemini key", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Entry(setup, textvariable=self.gemini_var, show="*", width=40).grid(row=0, column=3, sticky="ew", padx=(0, 12))
        self.connect_button = ttk.Button(setup, text="Connect", command=self._connect)
        self.connect_button.grid(row=0, column=4, sticky="e")

        channels = ttk.Frame(root, style="Panel.TFrame", padding=14)
        channels.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        for column in range(1, 6, 2):
            channels.columnconfigure(column, weight=1)
        ttk.Label(channels, text="Server", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.guild_var = tk.StringVar()
        self.guild_combo = ttk.Combobox(
            channels,
            textvariable=self.guild_var,
            state="readonly",
            style="Dark.TCombobox",
            height=16,
        )
        self.guild_combo.configure(postcommand=lambda: self.after_idle(lambda: self._style_combobox_dropdown(self.guild_combo)))
        self.guild_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.guild_combo.bind("<<ComboboxSelected>>", self._on_guild_selected)
        ttk.Label(channels, text="Voice", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.voice_var = tk.StringVar()
        self.voice_combo = ttk.Combobox(
            channels,
            textvariable=self.voice_var,
            state="readonly",
            style="Dark.TCombobox",
            height=16,
        )
        self.voice_combo.configure(postcommand=lambda: self.after_idle(lambda: self._style_combobox_dropdown(self.voice_combo)))
        self.voice_combo.grid(row=0, column=3, sticky="ew", padx=(0, 12))
        ttk.Label(channels, text="Text", style="Panel.TLabel").grid(row=0, column=4, sticky="w", padx=(0, 8))
        self.text_var = tk.StringVar()
        self.text_combo = ttk.Combobox(
            channels,
            textvariable=self.text_var,
            state="readonly",
            style="Dark.TCombobox",
            height=16,
        )
        self.text_combo.configure(postcommand=lambda: self.after_idle(lambda: self._style_combobox_dropdown(self.text_combo)))
        self.text_combo.grid(row=0, column=5, sticky="ew")

        actions = ttk.Frame(root, style="Root.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.start_bot_button = ttk.Button(actions, text="Start Nara", style="Accent.TButton", command=self._start_bot, state="disabled")
        self.stop_bot_button = ttk.Button(actions, text="Stop Nara", style="Danger.TButton", command=self._stop_bot, state="disabled")
        self.start_record_button = ttk.Button(actions, text="Start Recording", command=self._start_recording, state="disabled")
        self.stop_summary_button = ttk.Button(actions, text="Stop + Summary", command=lambda: self._stop_recording(True), state="disabled")
        self.stop_transcript_button = ttk.Button(actions, text="Stop Transcript Only", command=lambda: self._stop_recording(False), state="disabled")
        self.audio_test_button = ttk.Button(actions, text="Audio Test 5s", command=self._audio_test, state="disabled")
        self.leave_button = ttk.Button(actions, text="Leave Voice", command=self._leave_voice, state="disabled")
        self.status_button = ttk.Button(actions, text="Refresh Status", command=self._refresh_status, state="disabled")
        for index, button in enumerate(
            [
                self.start_bot_button,
                self.stop_bot_button,
                self.start_record_button,
                self.stop_summary_button,
                self.stop_transcript_button,
                self.audio_test_button,
                self.leave_button,
                self.status_button,
            ]
        ):
            button.grid(row=0, column=index, padx=(0, 8), sticky="w")

        status = ttk.Frame(root, style="Panel.TFrame", padding=14)
        status.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        for column in range(4):
            status.columnconfigure(column, weight=1)
        self.connected_var = tk.StringVar(value="Bot: stopped")
        self.voice_status_var = tk.StringVar(value="Voice: not connected")
        self.recording_var = tk.StringVar(value="Recording: no")
        self.output_var = tk.StringVar(value="Output: not configured")
        self.audio_var = tk.StringVar(value="Audio: no packets yet")
        self.last_result_var = tk.StringVar(value="Last result: none")
        for index, var in enumerate(
            [
                self.connected_var,
                self.voice_status_var,
                self.recording_var,
                self.output_var,
                self.audio_var,
                self.last_result_var,
            ]
        ):
            ttk.Label(status, textvariable=var, style="Status.TLabel").grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 16), pady=3)

        lower = ttk.Frame(root, style="Root.TFrame")
        lower.grid(row=5, column=0, sticky="nsew")
        lower.columnconfigure(0, weight=3)
        lower.columnconfigure(1, weight=1)
        lower.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            lower,
            height=18,
            bg="#111113",
            fg="#f5f5f7",
            insertbackground="#f5f5f7",
            relief="flat",
            font=("Consolas", 9),
            wrap="word",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.log_text.configure(state="disabled")

        side = ttk.Frame(lower, style="Panel.TFrame", padding=12)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        ttk.Button(side, text="List Speakers", command=self._list_speakers, state="disabled").grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.list_speakers_button = side.grid_slaves(row=0, column=0)[0]
        ttk.Label(side, text="Speaker user ID", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        self.speaker_id_var = tk.StringVar()
        ttk.Entry(side, textvariable=self.speaker_id_var).grid(row=2, column=0, sticky="ew", pady=(2, 8))
        ttk.Label(side, text="Preferred name", style="Panel.TLabel").grid(row=3, column=0, sticky="w")
        self.speaker_name_var = tk.StringVar()
        ttk.Entry(side, textvariable=self.speaker_name_var).grid(row=4, column=0, sticky="ew", pady=(2, 8))
        self.save_speaker_button = ttk.Button(side, text="Save Speaker Name", command=self._save_speaker_name, state="disabled")
        self.save_speaker_button.grid(row=5, column=0, sticky="ew")

        self._append_log("Open this window, connect, start Nara, then control recording here.")

    def _connect(self) -> None:
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("Missing token", "Paste your Discord bot token first.")
            return
        if self.is_loading:
            return
        self.is_loading = True
        self.connect_button.configure(state="disabled")
        self._append_log("Connecting to Discord and loading servers...")
        threading.Thread(target=self._load_snapshot_worker, args=(token,), daemon=True).start()

    def _load_snapshot_worker(self, token: str) -> None:
        try:
            snapshot = asyncio.run(fetch_discord_snapshot(token))
            self.worker_queue.put(("snapshot", {"token": token, "snapshot": snapshot}))
        except Exception as exc:
            self.worker_queue.put(("error", f"Could not connect to Discord: {exc}"))

    def _populate_guilds(self) -> None:
        self.guild_by_label.clear()
        labels: list[str] = []
        for guild in self.snapshot:
            label = f"{guild['name']} ({guild['id']})"
            self.guild_by_label[label] = guild
            labels.append(label)
        self.guild_combo.configure(values=labels)
        if labels:
            self.guild_combo.current(self._preferred_index(labels, self.last_selection.get("guild_id")))
            self._on_guild_selected()
            total_voice = sum(len(guild.get("voice_channels", [])) for guild in self.snapshot)
            total_text = sum(len(guild.get("text_channels", [])) for guild in self.snapshot)
            self._append_log(f"Loaded {len(labels)} server(s), {total_voice} voice channel(s), and {total_text} text channel(s).")
            for guild in self.snapshot:
                voice_count = len(guild.get("voice_channels", []))
                text_count = len(guild.get("text_channels", []))
                self._append_log(f"- {guild['name']}: {voice_count} voice, {text_count} text")
        else:
            self._append_log("The bot is not in any servers yet. Invite it first, then reconnect.")
        self._refresh_button_state()

    def _on_guild_selected(self, _event: object | None = None) -> None:
        guild = self.guild_by_label.get(self.guild_var.get())
        self.voice_by_label.clear()
        self.text_by_label.clear()
        if not guild:
            self._refresh_button_state()
            return
        voice_labels = self._channel_labels(guild.get("voice_channels", []), self.voice_by_label)
        text_labels = self._channel_labels(guild.get("text_channels", []), self.text_by_label)
        self.voice_combo.configure(values=voice_labels)
        self.text_combo.configure(values=text_labels)
        self.voice_combo.current(self._preferred_index(voice_labels, self.last_selection.get("voice_channel_id"))) if voice_labels else self.voice_var.set("")
        self.text_combo.current(self._preferred_index(text_labels, self.last_selection.get("text_channel_id"))) if text_labels else self.text_var.set("")
        self._append_log(f"Selected {guild['name']}: {len(voice_labels)} voice channel(s), {len(text_labels)} text channel(s).")
        self._refresh_button_state()

    def _start_bot(self) -> None:
        guild = self.guild_by_label.get(self.guild_var.get())
        voice = self.voice_by_label.get(self.voice_var.get())
        text = self.text_by_label.get(self.text_var.get())
        token = self.token_var.get().strip()
        gemini = self.gemini_var.get().strip()
        if not guild or not voice or not text:
            messagebox.showerror("Missing selection", "Select a server, voice channel, and text output channel.")
            return
        if not token or not gemini:
            messagebox.showerror("Missing keys", "Discord token and Gemini key are required.")
            return
        update_env_values(self.project_root / ".env", {"DISCORD_TOKEN": token, "GEMINI_API_KEY": gemini})
        self._save_last_selection(guild, voice, text)
        try:
            config = load_config(require_secrets=True, project_root=self.project_root)
        except ConfigError as exc:
            messagebox.showerror("Config error", str(exc))
            return
        config.discord_token = token
        config.gemini_api_key = gemini
        config.guild_id = int(guild["id"])
        config.default_voice_channel_id = int(voice["id"])
        config.default_text_channel_id = int(text["id"])
        logger = setup_logging(self.project_root / "logs")
        gui_handler = GuiLogHandler(self.worker_queue)
        gui_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(gui_handler)
        self.bot_worker = BotWorker(config, logger, self.worker_queue)
        self.bot_worker.start()
        self.connected_var.set("Bot: starting")
        self._append_log(f"Starting Nara for {guild['name']} / voice #{voice['name']} / text #{text['name']}")
        self._refresh_button_state()

    def _stop_bot(self) -> None:
        if self.bot_worker:
            self._append_log("Stopping Nara...")
            self.bot_worker.stop()

    def _start_recording(self) -> None:
        self._submit("Start recording", lambda runner: runner.start_recording())

    def _stop_recording(self, summarize: bool) -> None:
        label = "Stop recording with summary" if summarize else "Stop recording transcript only"
        self._submit(label, lambda runner: runner.stop_recording(summarize=summarize))

    def _audio_test(self) -> None:
        self._append_log("Starting 5 second audio test. Speak now.")
        self._submit("Audio test", lambda runner: runner.run_audio_check(5))

    def _leave_voice(self) -> None:
        self._submit("Leave voice", lambda runner: runner.leave_current_channel())

    def _refresh_status(self) -> None:
        self._submit("Refresh status", lambda runner: runner.status_snapshot())

    def _list_speakers(self) -> None:
        async def command(runner: NaraBotRunner) -> list[str]:
            return runner.list_speakers_lines(limit=100)

        self._submit("List speakers", command)

    def _save_speaker_name(self) -> None:
        user_id = self.speaker_id_var.get().strip()
        name = self.speaker_name_var.get().strip()
        if not user_id or not name:
            messagebox.showerror("Missing speaker info", "Enter both speaker user ID and preferred name.")
            return

        async def command(runner: NaraBotRunner):
            return runner.set_speaker_name(user_id, name)

        self._submit("Save speaker name", command)

    def _submit(self, label: str, factory: Callable[[NaraBotRunner], Any]) -> None:
        if not self.bot_worker or not self.bot_worker.is_alive:
            self._append_log("Start Nara before using bot commands.")
            return
        self._append_log(f"Command: {label}")
        self.bot_worker.submit(label, factory)

    def _poll_queue(self) -> None:
        while True:
            try:
                kind, payload = self.worker_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "snapshot":
                self.is_loading = False
                self.connect_button.configure(state="normal")
                self.snapshot = payload["snapshot"]
                update_env_values(self.project_root / ".env", {"DISCORD_TOKEN": payload["token"]})
                self._populate_guilds()
            elif kind == "error":
                self.is_loading = False
                self.connect_button.configure(state="normal")
                self._append_log(str(payload))
                messagebox.showerror("Nara error", str(payload))
            elif kind == "log":
                self._append_log(str(payload))
            elif kind == "bot_event":
                event_kind, event_payload = payload
                self._handle_bot_event(event_kind, event_payload)
            elif kind == "command_done":
                label, future = payload
                self._handle_command_done(label, future)
            elif kind == "bot_stopped":
                self.connected_var.set("Bot: stopped")
                self.recording_var.set("Recording: no")
                self._append_log("Nara stopped.")
                self._refresh_button_state()
        if not self.is_closing:
            self.poll_after_id = self.after(100, self._poll_queue)

    def _handle_bot_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "status":
            self._apply_status(payload)
        elif kind == "voice_event":
            name = payload.get("display_name", "Unknown")
            event_type = payload.get("event_type", "voice")
            before = payload.get("before_channel") or "-"
            after = payload.get("after_channel") or "-"
            self._append_log(f"Voice {event_type}: {name} [{before} -> {after}]")
        elif kind == "audio":
            user_id = payload.get("user_id", "unknown")
            kb = int(payload.get("audio_bytes", 0)) // 1024
            elapsed = payload.get("elapsed", 0)
            self.audio_var.set(f"Audio: hearing {user_id}, {kb} KB at {elapsed}s")
            self._append_log(f"Audio received from {user_id}: {kb} KB")
        elif kind == "recording":
            if payload.get("crashed"):
                self.recording_var.set("Recording: stopped (error) - press Stop to save partial audio")
                self._append_log(f"Recording stopped unexpectedly: {payload.get('error')}")
            elif payload.get("active"):
                self.recording_var.set(f"Recording: yes ({payload.get('session_id')})")
            elif payload.get("processing"):
                self.recording_var.set("Recording: processing")
            else:
                self.recording_var.set("Recording: no")
                if payload.get("transcript_dir"):
                    self.last_result_var.set(f"Last result: {payload.get('transcript_dir')}")
        elif kind == "audio_warning":
            self.audio_var.set(f"Audio: {payload.get('message')}")
            self._append_log(f"Warning: {payload.get('message')}")
        elif kind == "audio_test":
            if payload.get("active"):
                self.audio_var.set("Audio: test running")
            else:
                self._append_audio_test_result(payload)
        self._refresh_button_state()

    def _handle_command_done(self, label: str, future: Any) -> None:
        try:
            result = future.result()
        except Exception as exc:
            self._append_log(f"{label} failed: {exc}")
            messagebox.showerror(label, str(exc))
            return
        self._append_log(f"{label} finished.")
        if isinstance(result, dict) and "connected" in result:
            self._apply_status(result)
        elif isinstance(result, dict) and "speakers" in result:
            self._append_audio_test_result(result)
        elif isinstance(result, list):
            self._append_log("\n".join(result))
        elif hasattr(result, "session_id"):
            self.last_result_var.set(f"Last result: {getattr(result, 'transcript_dir', '')}")
            self._append_log(f"Session {result.session_id} saved to {result.transcript_dir}")
        elif result is not None:
            self._append_log(str(result))
        self._refresh_button_state()

    def _append_audio_test_result(self, payload: dict[str, Any]) -> None:
        speakers = payload.get("speakers", [])
        if not speakers:
            self.audio_var.set("Audio: no speaker audio captured")
            self._append_log("Audio test finished: no usable speaker audio captured.")
            return
        self.audio_var.set(f"Audio: heard {len(speakers)} speaker(s)")
        self._append_log("Audio test captured:")
        for item in speakers:
            speaker = item.get("speaker") or item.get("speaker_id") or "Unknown"
            duration = float(item.get("duration_seconds") or 0)
            kb = int(item.get("audio_bytes") or 0) // 1024
            self._append_log(f"- {speaker}: {duration:.1f}s, {kb} KB")

    def _apply_status(self, status: dict[str, Any]) -> None:
        self.connected_var.set("Bot: connected" if status.get("connected") else "Bot: online, no voice")
        self.voice_status_var.set(f"Voice: {status.get('voice_channel') or 'not connected'}")
        self.recording_var.set("Recording: yes" if status.get("recording") else "Recording: no")
        self.output_var.set(f"Output: {status.get('output_channel') or 'not configured'}")
        self._refresh_button_state()

    def _refresh_button_state(self) -> None:
        has_selection = bool(self.guild_var.get() and self.voice_var.get() and self.text_var.get())
        running = bool(self.bot_worker and self.bot_worker.is_alive)
        self.start_bot_button.configure(state="normal" if has_selection and not running else "disabled")
        self.stop_bot_button.configure(state="normal" if running else "disabled")
        for button in [
            self.start_record_button,
            self.stop_summary_button,
            self.stop_transcript_button,
            self.audio_test_button,
            self.leave_button,
            self.status_button,
            self.list_speakers_button,
            self.save_speaker_button,
        ]:
            button.configure(state="normal" if running else "disabled")

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _channel_labels(self, channels: list[dict[str, Any]], target: dict[str, dict[str, Any]]) -> list[str]:
        labels: list[str] = []
        for channel in channels:
            prefix = f"{channel['category']} / " if channel.get("category") else ""
            suffix = "" if channel.get("usable", True) else " - missing permissions"
            label = f"{prefix}{channel['name']} ({channel['id']}){suffix}"
            target[label] = channel
            labels.append(label)
        return labels

    def _style_combobox_dropdown(self, combobox: ttk.Combobox) -> None:
        try:
            popdown = combobox.tk.call("ttk::combobox::PopdownWindow", str(combobox))
            listbox = f"{popdown}.f.l"
            combobox.tk.call(listbox, "configure", "-background", "#242426")
            combobox.tk.call(listbox, "configure", "-foreground", "#f5f5f7")
            combobox.tk.call(listbox, "configure", "-selectbackground", "#0a84ff")
            combobox.tk.call(listbox, "configure", "-selectforeground", "#ffffff")
        except tk.TclError:
            return

    def _save_last_selection(self, guild: dict[str, Any], voice: dict[str, Any], text: dict[str, Any]) -> None:
        path = self.project_root / "data" / "runtime" / "last_channel_selection.json"
        ensure_dir(path.parent)
        write_json(
            path,
            {
                "guild_id": str(guild["id"]),
                "guild_name": str(guild["name"]),
                "voice_channel_id": str(voice["id"]),
                "voice_channel_name": str(voice["name"]),
                "text_channel_id": str(text["id"]),
                "text_channel_name": str(text["name"]),
            },
        )
        self.last_selection = self._load_last_selection()

    def _load_last_selection(self) -> dict[str, str]:
        path = self.project_root / "data" / "runtime" / "last_channel_selection.json"
        if not path.exists():
            return {}
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}

    @staticmethod
    def _preferred_index(labels: list[str], preferred_id: str | None) -> int:
        if preferred_id:
            needle = f"({preferred_id})"
            for index, label in enumerate(labels):
                if needle in label:
                    return index
        return 0

    def _close(self) -> None:
        self.is_closing = True
        if self.poll_after_id is not None:
            try:
                self.after_cancel(self.poll_after_id)
            except tk.TclError:
                pass
            self.poll_after_id = None
        if self.bot_worker and self.bot_worker.is_alive:
            self._append_log("Closing window and stopping Nara...")
            self.bot_worker.stop()
        self.after(500, self.destroy)
