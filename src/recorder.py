from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio_utils import AudioProcessingError, combine_wav_files, is_empty_audio
from .utils import ensure_dir, safe_filename, utcish_now_id


class RecordingError(RuntimeError):
    pass


@dataclass(slots=True)
class RecordedSpeakerFile:
    speaker: str
    user_id: int | None
    raw_path: Path
    speaker_id: str | None = None
    discord_display_name: str = ""


@dataclass(slots=True)
class RecordedAudioBundle:
    session_id: str
    started_at: datetime
    stopped_at: datetime
    recording_dir: Path
    speaker_files: list[RecordedSpeakerFile] = field(default_factory=list)
    combined_raw_path: Path | None = None


class PycordVoiceRecorder:
    def __init__(self, recordings_root: Path) -> None:
        self.recordings_root = recordings_root
        self.session_id: str | None = None
        self.started_at: datetime | None = None
        self.recording_dir: Path | None = None
        self._sink: Any | None = None
        self._finished_future: asyncio.Future[Any] | None = None

    @property
    def is_recording(self) -> bool:
        return self._sink is not None and self._finished_future is not None and not self._finished_future.done()

    async def start(self, voice_client: Any) -> str:
        if self.is_recording:
            raise RecordingError("Recording is already in progress.")
        if voice_client is None or not getattr(voice_client, "is_connected", lambda: False)():
            raise RecordingError("Bot is not connected to a voice channel.")

        try:
            import discord
        except ImportError as exc:
            raise RecordingError("py-cord is not installed. Run python setup_nara.py first.") from exc

        self.session_id = utcish_now_id()
        self.started_at = datetime.now()
        self.recording_dir = ensure_dir(self.recordings_root / self.session_id)
        self._sink = discord.sinks.WaveSink()
        loop = asyncio.get_running_loop()
        self._finished_future = loop.create_future()

        async def finished_callback(sink: Any, *args: Any) -> None:
            if self._finished_future and not self._finished_future.done():
                self._finished_future.set_result(sink)

        try:
            voice_client.start_recording(self._sink, finished_callback)
        except Exception as exc:
            self._sink = None
            self._finished_future = None
            raise RecordingError(
                "Discord voice recording could not start. Check that Pycord voice receive is supported, "
                "the bot has Connect/Speak permissions, and the voice channel is not blocking recording."
            ) from exc

        return self.session_id

    async def stop(self, voice_client: Any, bot: Any) -> RecordedAudioBundle:
        if not self.is_recording or self._finished_future is None:
            raise RecordingError("Recording has not started yet.")
        if voice_client is None:
            raise RecordingError("Bot is not connected to a voice channel.")

        try:
            voice_client.stop_recording()
        except Exception as exc:
            raise RecordingError("Discord voice recording could not stop cleanly.") from exc

        try:
            sink = await asyncio.wait_for(self._finished_future, timeout=30)
        except asyncio.TimeoutError as exc:
            raise RecordingError("Timed out while waiting for Discord audio data after stopping recording.") from exc
        finally:
            self._finished_future = None
            self._sink = None

        if not self.session_id or not self.started_at or not self.recording_dir:
            raise RecordingError("Recorder state was lost before files could be saved.")

        bundle = await self._save_sink_audio(sink, bot)
        self.session_id = None
        self.started_at = None
        self.recording_dir = None
        return bundle

    async def _save_sink_audio(self, sink: Any, bot: Any) -> RecordedAudioBundle:
        audio_data = getattr(sink, "audio_data", {}) or {}
        speaker_files: list[RecordedSpeakerFile] = []
        assert self.recording_dir is not None
        assert self.session_id is not None
        assert self.started_at is not None

        for raw_user_id, audio in audio_data.items():
            user_id = int(raw_user_id) if str(raw_user_id).isdigit() else None
            discord_display_name = await resolve_speaker_name(bot, user_id)
            speaker_id = str(user_id) if user_id is not None else None
            speaker = discord_display_name if discord_display_name != "Speaker" else "Speaker"
            safe_speaker = safe_filename(f"user_{speaker_id}" if speaker_id else speaker)
            raw_path = self.recording_dir / f"{safe_speaker}_raw.wav"

            file_obj = getattr(audio, "file", None)
            if file_obj is None:
                continue
            try:
                file_obj.seek(0)
                data = file_obj.read()
            except Exception:
                continue
            if not data or len(data) < 2048:
                continue
            raw_path.write_bytes(data)
            if is_empty_audio(raw_path):
                raw_path.unlink(missing_ok=True)
                continue
            speaker_files.append(
                RecordedSpeakerFile(
                    speaker=speaker,
                    user_id=user_id,
                    raw_path=raw_path,
                    speaker_id=speaker_id,
                    discord_display_name=discord_display_name,
                )
            )

        if not speaker_files:
            raise RecordingError(
                "No usable audio was captured. This can happen if nobody spoke, the bot lacked voice permissions, "
                "or Discord voice receive was unavailable for that channel."
            )

        combined_raw_path = self.recording_dir / "combined_raw.wav"
        try:
            combined_raw = combine_wav_files([item.raw_path for item in speaker_files], combined_raw_path)
        except AudioProcessingError:
            combined_raw = None

        return RecordedAudioBundle(
            session_id=self.session_id,
            started_at=self.started_at,
            stopped_at=datetime.now(),
            recording_dir=self.recording_dir,
            speaker_files=speaker_files,
            combined_raw_path=combined_raw,
        )


async def resolve_speaker_name(bot: Any, user_id: int | None) -> str:
    if user_id is None:
        return "Speaker"
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except Exception:
            user = None
    if user is None:
        return f"Speaker {user_id}"
    display = getattr(user, "display_name", None) or getattr(user, "name", None)
    return str(display or f"Speaker {user_id}")
