from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any, Callable

from .config import NaraConfig
from .contacts import SpeakerContactBook
from .outputs import ObsidianVaultWriter, ProcessingResult, process_recording_bundle
from .recorder import PycordVoiceRecorder, RecordedAudioBundle, VoiceEvent


class BotRuntimeError(RuntimeError):
    pass


class NaraBotRunner:
    def __init__(
        self,
        config: NaraConfig,
        logger,
        ui_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        try:
            import discord
        except ImportError as exc:
            raise BotRuntimeError("py-cord is not installed. Run python setup_nara.py first.") from exc

        self.discord = discord
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True

        debug_guilds = [config.guild_id] if config.guild_id else None
        self.bot = discord.Bot(intents=intents, debug_guilds=debug_guilds)
        self.config = config
        self.logger = logger
        self.ui_callback = ui_callback
        self.recorder = PycordVoiceRecorder(
            config.recordings_dir,
            audio_callback=self._handle_audio_activity,
            event_callback=self._handle_recorder_event,
        )
        self.output_channel_id = config.default_text_channel_id
        self._ready_once = False
        self._audio_log_seen: dict[str, int] = {}
        self._audio_watchdog_task: asyncio.Task | None = None
        self._register_commands()

    async def run(self) -> None:
        self.logger.info("Bot started")
        try:
            await self.bot.start(self.config.discord_token)
        except self.discord.LoginFailure as exc:
            raise BotRuntimeError("Invalid Discord token. Check DISCORD_TOKEN in .env and try again.") from exc
        finally:
            if not self.bot.is_closed():
                with contextlib.suppress(Exception):
                    await self.bot.close()

    def _register_commands(self) -> None:
        discord = self.discord
        bot = self.bot

        @bot.event
        async def on_ready() -> None:
            if self._ready_once:
                return
            self._ready_once = True
            self.logger.info("Connected to Discord as %s", bot.user)
            self._emit_ui("status", {"connected": True, "user": str(bot.user)})
            try:
                await bot.sync_commands()
                self.logger.info("Slash commands synced")
            except Exception as exc:
                self.logger.warning("Slash command sync warning: %s", exc)

            if self.config.default_voice_channel_id:
                await self._auto_join_with_retry(self.config.default_voice_channel_id)
                self._emit_ui("status", await self.status_snapshot())
            else:
                self.logger.info("No default voice channel selected. Use the GUI channel picker or /join.")
                self._emit_ui("status", await self.status_snapshot())

        @bot.event
        async def on_voice_state_update(member: Any, before: Any, after: Any) -> None:
            await self.handle_voice_state_update(member, before, after)

        @bot.slash_command(name="join", description="Join a Discord voice channel by channel ID.")
        async def join(ctx: discord.ApplicationContext, voice_channel_id: str) -> None:
            try:
                channel_id = int(voice_channel_id.strip())
                channel = await self.join_channel_by_id(channel_id)
                await respond(ctx, f"Nara joined voice channel: {channel.name}")
            except Exception as exc:
                await respond(ctx, explain_error("Could not join voice channel", exc), ephemeral=True)

        @bot.slash_command(name="start_record", description="Start recording the current voice channel.")
        async def start_record(ctx: discord.ApplicationContext) -> None:
            try:
                session_id = await self.start_recording()
                await respond(ctx, f"Recording started. Session: `{session_id}`", ephemeral=True)
            except Exception as exc:
                await respond(ctx, explain_error("Could not start recording", exc), ephemeral=True)

        @bot.slash_command(name="stop_record", description="Stop recording and process the meeting.")
        async def stop_record(ctx: discord.ApplicationContext, summarize: bool = True) -> None:
            await ctx.defer()
            try:
                await ctx.followup.send("Nara stopped recording. Processing the meeting now.")
                result = await self.stop_recording(summarize=summarize)
                await self.send_processing_result_to_context(ctx, result, summarize)
            except Exception as exc:
                self.logger.exception("Stop/process failed")
                await ctx.followup.send(explain_error("Could not process recording", exc))

        @bot.slash_command(name="leave", description="Leave the current voice channel safely.")
        async def leave(ctx: discord.ApplicationContext) -> None:
            await ctx.defer()
            try:
                voice_client = self.current_voice_client(required=False)
                if voice_client is None:
                    await ctx.followup.send("Nara is not connected to a voice channel.")
                    return
                if self.recorder.is_recording:
                    await ctx.followup.send("Recording was active. Stopping it before leaving.")
                    result = await self.stop_recording(summarize=False)
                    await self.send_processing_result_to_context(ctx, result, include_summary=False)
                await self.leave_current_channel()
                await ctx.followup.send("Nara left the voice channel.")
            except Exception as exc:
                await ctx.followup.send(explain_error("Could not leave voice channel", exc))

        @bot.slash_command(name="status", description="Show Nara recording and output status.")
        async def status(ctx: discord.ApplicationContext) -> None:
            message = self.render_status(await self.status_snapshot())
            await respond(ctx, message, ephemeral=True)

        @bot.slash_command(name="set_output_channel", description="Set the text channel for Nara output files.")
        async def set_output_channel(ctx: discord.ApplicationContext, text_channel_id: str) -> None:
            try:
                channel = await self.fetch_channel_by_id(int(text_channel_id.strip()))
                if not isinstance(channel, discord.TextChannel):
                    raise BotRuntimeError("Text channel not found. Paste a Discord text channel ID.")
                self.output_channel_id = channel.id
                await respond(ctx, f"Output channel set to #{channel.name}", ephemeral=True)
            except Exception as exc:
                await respond(ctx, explain_error("Could not set output channel", exc), ephemeral=True)

        @bot.slash_command(name="set_speaker_name", description="Assign a preferred name to a Discord speaker user ID.")
        async def set_speaker_name(ctx: discord.ApplicationContext, user_id: str, name: str) -> None:
            try:
                cleaned_user_id = user_id.strip()
                cleaned_name = name.strip()
                if not cleaned_user_id.isdigit():
                    raise BotRuntimeError("User ID must be the numeric Discord user ID from the transcript.")
                if not cleaned_name:
                    raise BotRuntimeError("Name cannot be empty.")
                contact = self.set_speaker_name(cleaned_user_id, cleaned_name)
                await respond(
                    ctx,
                    f"Speaker `{cleaned_user_id}` is now saved as **{contact.preferred_name}**. "
                    f"Future transcript label: `{contact.transcript_label}`",
                    ephemeral=True,
                )
            except Exception as exc:
                await respond(ctx, explain_error("Could not set speaker name", exc), ephemeral=True)

        @bot.slash_command(name="list_speakers", description="List known Discord speaker IDs and preferred names.")
        async def list_speakers(ctx: discord.ApplicationContext) -> None:
            try:
                lines = self.list_speakers_lines(limit=25)
                if len(lines) == 1:
                    await respond(ctx, "No speaker contacts have been captured yet.", ephemeral=True)
                    return
                await respond(ctx, "\n".join(lines), ephemeral=True)
            except Exception as exc:
                await respond(ctx, explain_error("Could not list speaker contacts", exc), ephemeral=True)

        @bot.slash_command(name="help_nara", description="Show Nara usage help.")
        async def help_nara(ctx: discord.ApplicationContext) -> None:
            message = (
                "**Nara commands**\n"
                "`/join voice_channel_id` joins a voice channel by ID.\n"
                "`/start_record` starts recording and posts the consent reminder.\n"
                "`/stop_record summarize:true` stops, transcribes locally, cleans with Gemini, and creates summary files.\n"
                "`/stop_record summarize:false` only sends transcript output.\n"
                "`/set_output_channel text_channel_id` changes where files are sent.\n"
                "`/status` shows connection, recording, STT, and output settings.\n"
                "`/list_speakers` shows captured Discord speaker IDs.\n"
                "`/set_speaker_name user_id name` saves a preferred display name for future transcripts.\n"
                "`/leave` stops recording if needed and leaves the voice channel.\n\n"
                "Only raw audio stays local. Gemini receives transcript text only."
            )
            await respond(ctx, message, ephemeral=True)

    async def _auto_join_with_retry(self, channel_id: int, attempts: int = 3) -> None:
        delay = 1.5
        for attempt in range(1, attempts + 1):
            try:
                await self.join_channel_by_id(channel_id)
                self.logger.info("Joined default voice channel: %s", channel_id)
                return
            except Exception as exc:
                if attempt == attempts:
                    self.logger.error(
                        "Could not join default voice channel after %s attempt(s): %s", attempts, exc
                    )
                    return
                self.logger.warning(
                    "Auto-join attempt %s/%s failed (%s), retrying in %.1fs", attempt, attempts, exc, delay
                )
                await asyncio.sleep(delay)
                delay *= 2

    async def join_channel_by_id(self, channel_id: int) -> Any:
        channel = await self.fetch_channel_by_id(channel_id)
        if not isinstance(channel, (self.discord.VoiceChannel, self.discord.StageChannel)):
            raise BotRuntimeError("Voice channel not found. Paste a Discord voice channel ID.")

        guild = channel.guild
        me = guild.me or guild.get_member(self.bot.user.id)
        if me is None:
            raise BotRuntimeError("Bot member was not found in the guild. Reinvite the bot and try again.")
        permissions = channel.permissions_for(me)
        if not permissions.connect or not permissions.speak:
            raise BotRuntimeError("Missing Discord voice permissions. Grant Connect and Speak for this channel.")

        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected():
            await voice_client.move_to(channel)
        else:
            await channel.connect()
        return channel

    async def fetch_channel_by_id(self, channel_id: int) -> Any:
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception as exc:
            raise BotRuntimeError(f"Discord channel not found for ID {channel_id}.") from exc

    def current_voice_client(self, required: bool = True) -> Any:
        for guild in self.bot.guilds:
            voice_client = guild.voice_client
            if voice_client and voice_client.is_connected():
                return voice_client
        if required:
            raise BotRuntimeError("Bot is not connected to a voice channel. Use /join first.")
        return None

    async def resolve_output_channel(self, ctx: Any, allow_fallback: bool = False) -> Any:
        if self.output_channel_id:
            channel = await self.fetch_channel_by_id(self.output_channel_id)
            if isinstance(channel, self.discord.TextChannel):
                return channel
            if not allow_fallback:
                raise BotRuntimeError("Configured output text channel was not found.")
        if isinstance(ctx.channel, self.discord.TextChannel):
            return ctx.channel
        if allow_fallback:
            return None
        raise BotRuntimeError("No text output channel is configured. Use /set_output_channel.")

    async def start_recording(self, announce: bool = True) -> str:
        voice_client = self.current_voice_client()
        session_id = await self.recorder.start(voice_client)
        self._record_initial_voice_members(voice_client)
        self._audio_log_seen = {}
        self._start_audio_watchdog(session_id)
        if announce:
            output_channel = await self.resolve_output_channel_from_config()
            await output_channel.send(
                "Nara is now recording this voice channel. Please make sure everyone in the meeting is aware."
            )
        self.logger.info("Recording started: %s", session_id)
        self._emit_ui("recording", {"active": True, "session_id": session_id})
        self._emit_ui("status", await self.status_snapshot())
        return session_id

    async def stop_recording(self, summarize: bool = True, announce: bool = True) -> ProcessingResult:
        self._stop_audio_watchdog()
        voice_client = self.current_voice_client()
        bundle = await self.recorder.stop(voice_client, self.bot)
        self.logger.info("Recording stopped: %s", bundle.session_id)
        if bundle.crash_note:
            self.logger.warning(bundle.crash_note)
        self._emit_ui("recording", {"active": False, "session_id": bundle.session_id, "processing": True})
        result = await asyncio.to_thread(process_recording_bundle, bundle, self.config, self.logger, summarize)
        if announce:
            await self.send_processing_result_to_channel(await self.resolve_output_channel_from_config(), result, summarize)
        self._emit_ui(
            "recording",
            {
                "active": False,
                "processing": False,
                "session_id": result.session_id,
                "transcript_dir": str(result.transcript_dir),
                "output_dir": str(result.output_dir),
            },
        )
        self._emit_ui("status", await self.status_snapshot())
        return result

    async def run_audio_check(self, seconds: int = 5) -> dict[str, Any]:
        if self.recorder.is_recording:
            raise BotRuntimeError("Stop the active recording before running an audio test.")
        seconds = max(3, min(seconds, 15))
        voice_client = self.current_voice_client()
        session_id = await self.recorder.start(voice_client)
        self._record_initial_voice_members(voice_client)
        self.logger.info("Audio test started for %s seconds: %s", seconds, session_id)
        self._emit_ui("audio_test", {"active": True, "session_id": session_id, "seconds": seconds})
        await asyncio.sleep(seconds)
        bundle = await self.recorder.stop(voice_client, self.bot)
        speakers = [
            {
                "speaker": item.speaker,
                "speaker_id": item.speaker_id,
                "duration_seconds": item.duration_seconds,
                "audio_bytes": item.audio_bytes,
                "path": str(item.raw_path),
            }
            for item in bundle.speaker_files
        ]
        result = {
            "session_id": bundle.session_id,
            "speakers": speakers,
            "recording_dir": str(bundle.recording_dir),
            "voice_events": [event.to_json() for event in bundle.voice_events],
        }
        self.logger.info("Audio test captured %s speaker track(s)", len(speakers))
        self._emit_ui("audio_test", {"active": False, **result})
        self._emit_ui("status", await self.status_snapshot())
        return result

    async def leave_current_channel(self) -> None:
        voice_client = self.current_voice_client(required=False)
        if voice_client is None:
            return
        self._stop_audio_watchdog()
        await voice_client.disconnect(force=False)
        self.logger.info("Left voice channel")
        self._emit_ui("status", await self.status_snapshot())

    async def shutdown(self) -> None:
        self._stop_audio_watchdog()
        voice_client = self.current_voice_client(required=False)
        if voice_client is not None and self.recorder.is_recording:
            self.logger.warning("Shutting down while a recording is active; stopping capture without processing.")
            with contextlib.suppress(Exception):
                await self.recorder.stop(voice_client, self.bot)
        if voice_client is not None and voice_client.is_connected():
            with contextlib.suppress(Exception):
                await voice_client.disconnect(force=True)
        if not self.bot.is_closed():
            await self.bot.close()

    async def resolve_output_channel_from_config(self) -> Any:
        if self.output_channel_id:
            channel = await self.fetch_channel_by_id(self.output_channel_id)
            if isinstance(channel, self.discord.TextChannel):
                return channel
        raise BotRuntimeError("No text output channel is configured.")

    async def status_snapshot(self) -> dict[str, Any]:
        voice_client = self.current_voice_client(required=False)
        connected = voice_client is not None and voice_client.is_connected()
        voice_channel = getattr(getattr(voice_client, "channel", None), "name", "Not connected")
        output_name = "Not configured"
        if self.output_channel_id:
            with contextlib.suppress(Exception):
                output_channel = await self.fetch_channel_by_id(self.output_channel_id)
                output_name = getattr(output_channel, "name", output_name)
        return {
            "connected": connected,
            "ready": self._ready_once,
            "voice_channel": voice_channel,
            "voice_channel_id": getattr(getattr(voice_client, "channel", None), "id", None),
            "recording": self.recorder.is_recording,
            "output_channel": output_name,
            "output_channel_id": self.output_channel_id,
            "stt_engine": self.config.stt_engine,
            "model_size": self.config.stt_model_size,
            "languages": ",".join(self.config.stt_allowed_languages),
            "contacts_file": str(self.config.contacts_file),
        }

    def render_status(self, snapshot: dict[str, Any]) -> str:
        return (
            "**Nara status**\n"
            f"- Connected: {'yes' if snapshot.get('connected') else 'no'}\n"
            f"- Voice channel: {snapshot.get('voice_channel')}\n"
            f"- Recording: {'yes' if snapshot.get('recording') else 'no'}\n"
            f"- Output text channel: {snapshot.get('output_channel')}\n"
            f"- STT engine: {snapshot.get('stt_engine')}\n"
            f"- Model size: {snapshot.get('model_size')}\n"
            f"- Allowed languages: {snapshot.get('languages')}\n"
            f"- Speaker contacts: {snapshot.get('contacts_file')}"
        )

    def set_speaker_name(self, user_id: str, name: str):
        contact_book = SpeakerContactBook(self.config.contacts_file).load()
        contact = contact_book.set_preferred_name(user_id, name)
        ObsidianVaultWriter(self.config.obsidian_vault_path, contact_book=contact_book).sync_contacts_index()
        self._emit_ui("speakers", {"updated": user_id, "name": name})
        return contact

    def list_speakers_lines(self, limit: int = 25) -> list[str]:
        contact_book = SpeakerContactBook(self.config.contacts_file).load()
        contacts = contact_book.all_contacts()
        lines = ["**Known Nara speakers**"]
        for contact in contacts[:limit]:
            preferred = contact.preferred_name or "Not set"
            discord_name = contact.discord_display_name or "Unknown"
            relationship = f" - relationship: {contact.relationship}" if contact.relationship else ""
            lines.append(f"- `{contact.discord_user_id}` - preferred: {preferred} - Discord: {discord_name}{relationship}")
        if len(contacts) > limit:
            lines.append(f"- Showing {limit} of {len(contacts)} contacts. Full file: `{self.config.contacts_file}`")
        return lines

    async def handle_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        if getattr(member, "bot", False):
            return
        before_channel = getattr(before, "channel", None)
        after_channel = getattr(after, "channel", None)
        before_id = getattr(before_channel, "id", None)
        after_id = getattr(after_channel, "id", None)
        target_ids = self._target_voice_channel_ids()
        if before_id not in target_ids and after_id not in target_ids:
            return

        if before_id == after_id:
            return
        if after_id in target_ids and before_id not in target_ids:
            event_type = "joined"
        elif before_id in target_ids and after_id not in target_ids:
            event_type = "left"
        elif after_id in target_ids:
            event_type = "moved_in"
        else:
            event_type = "moved_out"

        event = self._voice_event(event_type, member, before_channel, after_channel)
        self.logger.info("Voice %s: %s", event_type, event.to_line())
        self.recorder.add_voice_event(event)
        self._emit_ui("voice_event", event.to_json())
        self._emit_ui("status", await self.status_snapshot())

    def _target_voice_channel_ids(self) -> set[int]:
        ids: set[int] = set()
        if self.config.default_voice_channel_id:
            ids.add(self.config.default_voice_channel_id)
        voice_client = self.current_voice_client(required=False)
        channel_id = getattr(getattr(voice_client, "channel", None), "id", None)
        if channel_id:
            ids.add(int(channel_id))
        return ids

    def _record_initial_voice_members(self, voice_client: Any) -> None:
        channel = getattr(voice_client, "channel", None)
        for member in getattr(channel, "members", []) or []:
            if getattr(member, "bot", False):
                continue
            event = self._voice_event("present_at_start", member, None, channel)
            self.recorder.add_voice_event(event)
            self._emit_ui("voice_event", event.to_json())

    def _voice_event(self, event_type: str, member: Any, before_channel: Any, after_channel: Any) -> VoiceEvent:
        display_name = str(
            getattr(member, "display_name", None)
            or getattr(member, "name", None)
            or getattr(member, "global_name", None)
            or "Unknown"
        )
        user_id = getattr(member, "id", None)
        return VoiceEvent(
            timestamp=datetime.now(),
            event_type=event_type,
            user_id=int(user_id) if user_id else None,
            display_name=display_name,
            before_channel=str(getattr(before_channel, "name", "") or ""),
            after_channel=str(getattr(after_channel, "name", "") or ""),
        )

    def _handle_recorder_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "recording_crashed":
            self.logger.error("Recording stopped unexpectedly: %s", payload.get("error"))
            self._emit_ui(
                "recording",
                {
                    "active": False,
                    "session_id": payload.get("session_id"),
                    "crashed": True,
                    "error": payload.get("error"),
                },
            )

    def _start_audio_watchdog(self, session_id: str) -> None:
        self._stop_audio_watchdog()
        self._audio_watchdog_task = asyncio.ensure_future(self._audio_watchdog(session_id))

    def _stop_audio_watchdog(self) -> None:
        if self._audio_watchdog_task is not None and not self._audio_watchdog_task.done():
            self._audio_watchdog_task.cancel()
        self._audio_watchdog_task = None

    async def _audio_watchdog(self, session_id: str) -> None:
        last_total = -1
        stale_checks = 0
        try:
            while self.recorder.is_recording and self.recorder.session_id == session_id:
                await asyncio.sleep(5)
                if not (self.recorder.is_recording and self.recorder.session_id == session_id):
                    break
                total = self.recorder.total_audio_bytes()
                if total == 0:
                    message = (
                        "No audio has been received yet. Check that Nara has Connect/Speak permissions "
                        "and that someone in the channel is talking."
                    )
                    self.logger.warning("Audio watchdog: %s (session %s)", message, session_id)
                    self._emit_ui("audio_warning", {"session_id": session_id, "message": message})
                elif total == last_total:
                    stale_checks += 1
                    if stale_checks >= 4:
                        message = "No new audio in the last 20 seconds. Recording may have stalled."
                        self.logger.warning("Audio watchdog: %s (session %s)", message, session_id)
                        self._emit_ui("audio_warning", {"session_id": session_id, "message": message})
                        stale_checks = 0
                else:
                    stale_checks = 0
                last_total = total
        except asyncio.CancelledError:
            pass

    def _handle_audio_activity(self, user_id: str, audio_bytes: int, elapsed: float) -> None:
        previous = self._audio_log_seen.get(user_id, 0)
        if audio_bytes - previous < 48000:
            return
        self._audio_log_seen[user_id] = audio_bytes
        self._emit_ui(
            "audio",
            {
                "user_id": user_id,
                "audio_bytes": audio_bytes,
                "elapsed": round(elapsed, 2),
            },
        )

    def _emit_ui(self, kind: str, payload: dict[str, Any]) -> None:
        if self.ui_callback is not None:
            self.ui_callback(kind, payload)

    async def send_processing_result_to_context(self, ctx: Any, result: ProcessingResult, include_summary: bool) -> None:
        channel = await self.resolve_output_channel(ctx)
        await self.send_processing_result_to_channel(channel, result, include_summary)

    async def send_processing_result_to_channel(self, channel: Any, result: ProcessingResult, include_summary: bool) -> None:
        files = result.files.discord_uploads(include_summary)
        uploadable = [path for path in files if path.stat().st_size <= 8 * 1024 * 1024]
        too_large = [path for path in files if path not in uploadable]

        content = ""
        if result.crash_note:
            content += f"Note: {result.crash_note}\n\n"
        content += (
            "Nara finished processing the meeting.\n\n"
            "Files generated:\n"
            f"- transcript_clean.txt\n"
            f"- voice_events.json\n"
        )
        if include_summary:
            content += "- meeting_summary.md\n- meeting_minutes.md\n"
        if result.files.obsidian_meeting_note:
            content += f"\nObsidian note:\n{result.files.obsidian_meeting_note}\n"

        if too_large:
            content += "\nSome files were too large to upload to Discord. They were saved locally here:\n"
            content += f"{result.output_dir}\n{result.transcript_dir}\n"

        try:
            discord_files = [self.discord.File(str(path)) for path in uploadable]
            await channel.send(content, files=discord_files)
            self.logger.info("Files sent to Discord")
        except Exception:
            await channel.send(
                "The output files are too large to upload to Discord or Discord rejected the upload. "
                f"They were saved locally here:\n{result.output_dir}\n{result.transcript_dir}"
            )


async def respond(ctx: Any, message: str, ephemeral: bool = False) -> None:
    try:
        if getattr(ctx, "response", None) and ctx.response.is_done():
            await ctx.followup.send(message, ephemeral=ephemeral)
        else:
            await ctx.respond(message, ephemeral=ephemeral)
    except Exception:
        await ctx.send(message)


def explain_error(title: str, exc: Exception) -> str:
    detail = str(exc) or exc.__class__.__name__
    return (
        f"{title}.\n"
        f"What happened: {detail}\n"
        "What to do next: check the channel ID, bot permissions, .env settings, FFmpeg, and setup logs."
    )
