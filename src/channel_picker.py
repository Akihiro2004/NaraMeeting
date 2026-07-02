from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .config import update_env_values
from .utils import ensure_dir, write_json


@dataclass(slots=True)
class ChannelPickerResult:
    guild_id: int
    guild_name: str
    voice_channel_id: int
    voice_channel_name: str
    text_channel_id: int
    text_channel_name: str
    token: str


def run_channel_picker(project_root: Path, initial_token: str = "") -> ChannelPickerResult | None:
    app = ChannelPickerApp(project_root, initial_token)
    app.mainloop()
    return app.result


class ChannelPickerApp(tk.Tk):
    def __init__(self, project_root: Path, initial_token: str) -> None:
        super().__init__()
        self.project_root = project_root
        self.result: ChannelPickerResult | None = None
        self.snapshot: list[dict[str, Any]] = []
        self.last_selection = self._load_last_selection()
        self.guild_by_label: dict[str, dict[str, Any]] = {}
        self.voice_by_label: dict[str, dict[str, Any]] = {}
        self.text_by_label: dict[str, dict[str, Any]] = {}
        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.is_loading = False
        self.is_closing = False
        self.poll_after_id: str | None = None

        self.title("Nara")
        self.geometry("720x520")
        self.minsize(640, 460)
        self.configure(bg="#1c1c1e")
        self._build_style()
        self._build_ui(initial_token)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.poll_after_id = self.after(100, self._poll_worker)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#1c1c1e")
        style.configure("Panel.TFrame", background="#2c2c2e")
        style.configure("TLabel", background="#1c1c1e", foreground="#f5f5f7", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#1c1c1e", foreground="#a1a1a6", font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background="#2c2c2e", foreground="#f5f5f7", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#1c1c1e", foreground="#f5f5f7", font=("Segoe UI", 18, "bold"))
        style.configure("Status.TLabel", background="#1c1c1e", foreground="#a1a1a6", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10), padding=(14, 8), background="#3a3a3c", foreground="#f5f5f7")
        style.map("TButton", background=[("active", "#48484a"), ("disabled", "#2c2c2e")], foreground=[("disabled", "#6e6e73")])
        style.configure("Accent.TButton", background="#0a84ff", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#409cff"), ("disabled", "#2c2c2e")])
        style.configure("TEntry", fieldbackground="#3a3a3c", foreground="#f5f5f7", insertcolor="#f5f5f7", bordercolor="#48484a")
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
            fieldbackground=[("readonly", "#3a3a3c"), ("disabled", "#2c2c2e"), ("!disabled", "#3a3a3c")],
            foreground=[("readonly", "#f5f5f7"), ("disabled", "#8e8e93"), ("!disabled", "#f5f5f7")],
            selectbackground=[("readonly", "#3a3a3c"), ("!disabled", "#3a3a3c")],
            selectforeground=[("readonly", "#f5f5f7"), ("!disabled", "#f5f5f7")],
            background=[("active", "#48484a"), ("readonly", "#3a3a3c"), ("disabled", "#2c2c2e")],
            arrowcolor=[("disabled", "#8e8e93"), ("!disabled", "#f5f5f7")],
        )
        self.option_add("*TCombobox*Listbox.background", "#242426")
        self.option_add("*TCombobox*Listbox.foreground", "#f5f5f7")
        self.option_add("*TCombobox*Listbox.selectBackground", "#0a84ff")
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _build_ui(self, initial_token: str) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=24)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="Nara Channel Setup", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            root,
            text="Pick the server, voice channel, and text output channel from the bot account.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 20))

        panel = ttk.Frame(root, style="Panel.TFrame", padding=18)
        panel.grid(row=2, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="Bot token", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 12))
        self.token_var = tk.StringVar(value=initial_token)
        token_entry = ttk.Entry(panel, textvariable=self.token_var, show="*", width=64)
        token_entry.grid(row=0, column=1, sticky="ew", pady=(0, 12))
        self.connect_button = ttk.Button(panel, text="Connect", command=self._connect)
        self.connect_button.grid(row=0, column=2, sticky="e", padx=(12, 0), pady=(0, 12))

        ttk.Label(panel, text="Server", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=8)
        self.guild_var = tk.StringVar()
        self.guild_combo = ttk.Combobox(panel, textvariable=self.guild_var, state="readonly", style="Dark.TCombobox", height=16)
        self.guild_combo.configure(postcommand=lambda: self.after_idle(lambda: self._style_combobox_dropdown(self.guild_combo)))
        self.guild_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=8)
        self.guild_combo.bind("<<ComboboxSelected>>", self._on_guild_selected)

        ttk.Label(panel, text="Voice", style="Panel.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=8)
        self.voice_var = tk.StringVar()
        self.voice_combo = ttk.Combobox(panel, textvariable=self.voice_var, state="readonly", style="Dark.TCombobox", height=16)
        self.voice_combo.configure(postcommand=lambda: self.after_idle(lambda: self._style_combobox_dropdown(self.voice_combo)))
        self.voice_combo.grid(row=2, column=1, columnspan=2, sticky="ew", pady=8)

        ttk.Label(panel, text="Text output", style="Panel.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=8)
        self.text_var = tk.StringVar()
        self.text_combo = ttk.Combobox(panel, textvariable=self.text_var, state="readonly", style="Dark.TCombobox", height=16)
        self.text_combo.configure(postcommand=lambda: self.after_idle(lambda: self._style_combobox_dropdown(self.text_combo)))
        self.text_combo.grid(row=3, column=1, columnspan=2, sticky="ew", pady=8)

        self.status_var = tk.StringVar(value="Paste the bot token or use the token already loaded from .env, then connect.")
        ttk.Label(root, textvariable=self.status_var, style="Status.TLabel", wraplength=680).grid(row=3, column=0, sticky="w", pady=(14, 10))

        actions = ttk.Frame(root, style="Root.TFrame")
        actions.grid(row=4, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Cancel", command=self._cancel).grid(row=0, column=1, padx=(0, 8))
        self.start_button = ttk.Button(actions, text="Start Nara", style="Accent.TButton", command=self._start, state="disabled")
        self.start_button.grid(row=0, column=2)

        if initial_token:
            self.after(250, self._connect)

    def _connect(self) -> None:
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("Missing token", "Paste your Discord bot token first.")
            return
        if self.is_loading:
            return
        self.is_loading = True
        self._set_status("Connecting to Discord and loading servers...")
        self.connect_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        thread = threading.Thread(target=self._load_discord_snapshot_worker, args=(token,), daemon=True)
        thread.start()

    def _load_discord_snapshot_worker(self, token: str) -> None:
        try:
            snapshot = asyncio.run(fetch_discord_snapshot(token))
            self.worker_queue.put(("success", {"token": token, "snapshot": snapshot}))
        except Exception as exc:
            self.worker_queue.put(("error", str(exc) or exc.__class__.__name__))

    def _poll_worker(self) -> None:
        if self.is_closing:
            return
        try:
            kind, payload = self.worker_queue.get_nowait()
        except queue.Empty:
            self.poll_after_id = self.after(100, self._poll_worker)
            return

        self.is_loading = False
        self.connect_button.configure(state="normal")
        if kind == "success":
            self.snapshot = payload["snapshot"]
            token = payload["token"]
            update_env_values(self.project_root / ".env", {"DISCORD_TOKEN": token})
            self._populate_guilds()
        else:
            self._set_status(f"Could not connect: {payload}")
            messagebox.showerror("Discord connection failed", str(payload))
        if not self.is_closing:
            self.poll_after_id = self.after(100, self._poll_worker)

    def _populate_guilds(self) -> None:
        self.guild_by_label.clear()
        labels: list[str] = []
        for guild in self.snapshot:
            label = f"{guild['name']} ({guild['id']})"
            self.guild_by_label[label] = guild
            labels.append(label)
        self.guild_combo.configure(values=labels)
        if labels:
            preferred_index = self._preferred_index(labels, self.last_selection.get("guild_id"))
            self.guild_combo.current(preferred_index)
            self._on_guild_selected()
            total_voice = sum(len(guild.get("voice_channels", [])) for guild in self.snapshot)
            total_text = sum(len(guild.get("text_channels", [])) for guild in self.snapshot)
            self._set_status(
                f"Loaded {len(labels)} server(s), {total_voice} voice channel(s), and {total_text} text channel(s). "
                "Select channels, then start Nara."
            )
        else:
            self._set_status("The bot is not in any servers yet. Invite it first, then reconnect.")

    def _on_guild_selected(self, _event: object | None = None) -> None:
        guild = self.guild_by_label.get(self.guild_var.get())
        self.voice_by_label.clear()
        self.text_by_label.clear()
        if not guild:
            self.start_button.configure(state="disabled")
            return

        voice_labels = self._channel_labels(guild.get("voice_channels", []), self.voice_by_label)
        text_labels = self._channel_labels(guild.get("text_channels", []), self.text_by_label)
        self.voice_combo.configure(values=voice_labels)
        self.text_combo.configure(values=text_labels)
        if voice_labels:
            self.voice_combo.current(self._preferred_index(voice_labels, self.last_selection.get("voice_channel_id")))
        else:
            self.voice_var.set("")
        if text_labels:
            self.text_combo.current(self._preferred_index(text_labels, self.last_selection.get("text_channel_id")))
        else:
            self.text_var.set("")
        self._refresh_start_state()

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

    def _refresh_start_state(self) -> None:
        if self.guild_var.get() and self.voice_var.get() and self.text_var.get():
            self.start_button.configure(state="normal")
        else:
            self.start_button.configure(state="disabled")

    def _start(self) -> None:
        guild = self.guild_by_label.get(self.guild_var.get())
        voice = self.voice_by_label.get(self.voice_var.get())
        text = self.text_by_label.get(self.text_var.get())
        token = self.token_var.get().strip()
        if not guild or not voice or not text:
            messagebox.showerror("Missing selection", "Select a server, voice channel, and text output channel.")
            return
        if not voice.get("usable", True):
            messagebox.showwarning("Voice permissions", "Nara may not be able to join or speak in that voice channel.")
        if not text.get("usable", True):
            messagebox.showwarning("Text permissions", "Nara may not be able to send files in that text channel.")

        self.result = ChannelPickerResult(
            guild_id=int(guild["id"]),
            guild_name=str(guild["name"]),
            voice_channel_id=int(voice["id"]),
            voice_channel_name=str(voice["name"]),
            text_channel_id=int(text["id"]),
            text_channel_name=str(text["name"]),
            token=token,
        )
        self._save_last_selection()
        self.destroy()

    def _save_last_selection(self) -> None:
        if self.result is None:
            return
        path = self.project_root / "data" / "runtime" / "last_channel_selection.json"
        ensure_dir(path.parent)
        write_json(
            path,
            {
                "guild_id": str(self.result.guild_id),
                "guild_name": self.result.guild_name,
                "voice_channel_id": str(self.result.voice_channel_id),
                "voice_channel_name": self.result.voice_channel_name,
                "text_channel_id": str(self.result.text_channel_id),
                "text_channel_name": self.result.text_channel_name,
            },
        )

    def _load_last_selection(self) -> dict[str, str]:
        path = self.project_root / "data" / "runtime" / "last_channel_selection.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    @staticmethod
    def _preferred_index(labels: list[str], preferred_id: str | None) -> int:
        if preferred_id:
            needle = f"({preferred_id})"
            for index, label in enumerate(labels):
                if needle in label:
                    return index
        return 0

    def _cancel(self) -> None:
        self.is_closing = True
        if self.poll_after_id is not None:
            try:
                self.after_cancel(self.poll_after_id)
            except tk.TclError:
                pass
            self.poll_after_id = None
        self.result = None
        self.destroy()

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)


async def fetch_discord_snapshot(token: str) -> list[dict[str, Any]]:
    try:
        import discord
    except ImportError as exc:
        raise RuntimeError("py-cord is not installed. Run python setup_nara.py first.") from exc

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)
    ready = asyncio.Event()
    state: dict[str, Any] = {"error": None, "snapshot": []}

    @client.event
    async def on_ready() -> None:
        try:
            state["snapshot"] = await build_snapshot(client, discord)
        except Exception as exc:
            state["error"] = exc
        finally:
            ready.set()

    try:
        task = asyncio.create_task(client.start(token))
        ready_task = asyncio.create_task(ready.wait())
        done, pending = await asyncio.wait({task, ready_task}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if task in done and not ready.is_set():
            await task
        if ready_task not in done and not ready.is_set():
            raise asyncio.TimeoutError()
        await client.close()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for pending_task in pending:
            pending_task.cancel()
    except discord.LoginFailure as exc:
        raise RuntimeError("Invalid Discord bot token. Copy the token from Developer Portal > Bot.") from exc
    except asyncio.TimeoutError as exc:
        await client.close()
        raise RuntimeError("Timed out while connecting to Discord.") from exc
    except RuntimeError as exc:
        if "Session is closed" not in str(exc):
            raise

    if state["error"]:
        raise RuntimeError(str(state["error"]))
    return state["snapshot"]


async def build_snapshot(client: Any, discord: Any) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for guild in sorted(client.guilds, key=lambda item: item.name.lower()):
        try:
            channels = await guild.fetch_channels()
        except Exception:
            channels = list(guild.channels)
        voice_channels: list[dict[str, Any]] = []
        text_channels: list[dict[str, Any]] = []
        me = guild.me or guild.get_member(client.user.id)

        for channel in sorted(channels, key=lambda item: (str(getattr(getattr(item, "category", None), "name", "")), item.position, item.name.lower())):
            category = getattr(getattr(channel, "category", None), "name", "")
            permissions = channel.permissions_for(me) if me else None
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                usable = bool(permissions.connect and permissions.speak) if permissions else True
                voice_channels.append(channel_to_dict(channel, category, usable))
            elif isinstance(channel, discord.TextChannel):
                usable = bool(permissions.send_messages and permissions.attach_files) if permissions else True
                text_channels.append(channel_to_dict(channel, category, usable))

        snapshot.append(
            {
                "id": str(guild.id),
                "name": guild.name,
                "voice_channels": voice_channels,
                "text_channels": text_channels,
            }
        )
    return snapshot


def channel_to_dict(channel: Any, category: str, usable: bool) -> dict[str, Any]:
    return {
        "id": str(channel.id),
        "name": channel.name,
        "category": category,
        "usable": usable,
    }
