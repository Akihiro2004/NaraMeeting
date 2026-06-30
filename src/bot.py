from __future__ import annotations

import asyncio
from typing import Any

from .config import NaraConfig
from .contacts import SpeakerContactBook
from .outputs import ObsidianVaultWriter, ProcessingResult, process_recording_bundle
from .recorder import PycordVoiceRecorder


class BotRuntimeError(RuntimeError):
    pass


class NaraBotRunner:
    def __init__(self, config: NaraConfig, logger) -> None:
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
        self.recorder = PycordVoiceRecorder(config.recordings_dir)
        self.output_channel_id = config.default_text_channel_id
        self._ready_once = False
        self._register_commands()

    async def run(self) -> None:
        self.logger.info("Bot started")
        try:
            await self.bot.start(self.config.discord_token)
        except self.discord.LoginFailure as exc:
            raise BotRuntimeError("Invalid Discord token. Check DISCORD_TOKEN in .env and try again.") from exc

    def _register_commands(self) -> None:
        discord = self.discord
        bot = self.bot

        @bot.event
        async def on_ready() -> None:
            if self._ready_once:
                return
            self._ready_once = True
            self.logger.info("Connected to Discord as %s", bot.user)
            try:
                await bot.sync_commands()
                self.logger.info("Slash commands synced")
            except Exception as exc:
                self.logger.warning("Slash command sync warning: %s", exc)

            if self.config.default_voice_channel_id:
                try:
                    await self.join_channel_by_id(self.config.default_voice_channel_id)
                    self.logger.info("Joined default voice channel: %s", self.config.default_voice_channel_id)
                except Exception as exc:
                    self.logger.error("Could not join default voice channel: %s", exc)
            else:
                channel_id = await asyncio.to_thread(
                    input,
                    "Enter Discord voice channel ID for Nara to join, or press Enter to skip: ",
                )
                channel_id = channel_id.strip()
                if channel_id:
                    try:
                        await self.join_channel_by_id(int(channel_id))
                        self.logger.info("Joined voice channel from terminal: %s", channel_id)
                    except Exception as exc:
                        self.logger.error("Could not join requested voice channel: %s", exc)

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
                voice_client = self.current_voice_client()
                session_id = await self.recorder.start(voice_client)
                output_channel = await self.resolve_output_channel(ctx)
                await output_channel.send(
                    "Nara is now recording this voice channel. Please make sure everyone in the meeting is aware."
                )
                self.logger.info("Recording started: %s", session_id)
                await respond(ctx, f"Recording started. Session: `{session_id}`", ephemeral=True)
            except Exception as exc:
                await respond(ctx, explain_error("Could not start recording", exc), ephemeral=True)

        @bot.slash_command(name="stop_record", description="Stop recording and process the meeting.")
        async def stop_record(ctx: discord.ApplicationContext, summarize: bool = True) -> None:
            await ctx.defer()
            try:
                await ctx.followup.send("Nara stopped recording. Processing the meeting now.")
                voice_client = self.current_voice_client()
                bundle = await self.recorder.stop(voice_client, bot)
                self.logger.info("Recording stopped: %s", bundle.session_id)
                result = await asyncio.to_thread(process_recording_bundle, bundle, self.config, self.logger, summarize)
                await self.send_processing_result(ctx, result, summarize)
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
                    bundle = await self.recorder.stop(voice_client, bot)
                    result = await asyncio.to_thread(process_recording_bundle, bundle, self.config, self.logger, False)
                    await self.send_processing_result(ctx, result, include_summary=False)
                await voice_client.disconnect(force=False)
                self.logger.info("Left voice channel")
                await ctx.followup.send("Nara left the voice channel.")
            except Exception as exc:
                await ctx.followup.send(explain_error("Could not leave voice channel", exc))

        @bot.slash_command(name="status", description="Show Nara recording and output status.")
        async def status(ctx: discord.ApplicationContext) -> None:
            voice_client = self.current_voice_client(required=False)
            connected = voice_client is not None and voice_client.is_connected()
            voice_channel = getattr(getattr(voice_client, "channel", None), "name", "Not connected")
            output_channel = await self.resolve_output_channel(ctx, allow_fallback=True)
            output_name = getattr(output_channel, "name", "Not configured")
            message = (
                "**Nara status**\n"
                f"- Connected: {'yes' if connected else 'no'}\n"
                f"- Voice channel: {voice_channel}\n"
                f"- Recording: {'yes' if self.recorder.is_recording else 'no'}\n"
                f"- Output text channel: {output_name}\n"
                f"- STT engine: {self.config.stt_engine}\n"
                f"- Model size: {self.config.stt_model_size}\n"
                f"- Allowed languages: {','.join(self.config.stt_allowed_languages)}\n"
                f"- Speaker contacts: {self.config.contacts_file}"
            )
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
                contact_book = SpeakerContactBook(self.config.contacts_file).load()
                contact = contact_book.set_preferred_name(cleaned_user_id, cleaned_name)
                ObsidianVaultWriter(self.config.obsidian_vault_path, contact_book=contact_book).sync_contacts_index()
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
                contact_book = SpeakerContactBook(self.config.contacts_file).load()
                contacts = contact_book.all_contacts()
                if not contacts:
                    await respond(ctx, "No speaker contacts have been captured yet.", ephemeral=True)
                    return
                lines = ["**Known Nara speakers**"]
                for contact in contacts[:25]:
                    preferred = contact.preferred_name or "Not set"
                    discord_name = contact.discord_display_name or "Unknown"
                    relationship = f" - relationship: {contact.relationship}" if contact.relationship else ""
                    lines.append(
                        f"- `{contact.discord_user_id}` - preferred: {preferred} - Discord: {discord_name}{relationship}"
                    )
                if len(contacts) > 25:
                    lines.append(f"- Showing 25 of {len(contacts)} contacts. Full file: `{self.config.contacts_file}`")
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

    async def send_processing_result(self, ctx: Any, result: ProcessingResult, include_summary: bool) -> None:
        channel = await self.resolve_output_channel(ctx)
        files = result.files.discord_uploads(include_summary)
        uploadable = [path for path in files if path.stat().st_size <= 8 * 1024 * 1024]
        too_large = [path for path in files if path not in uploadable]

        content = (
            "Nara finished processing the meeting.\n\n"
            "Files generated:\n"
            f"- transcript_clean.txt\n"
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
