from __future__ import annotations

import asyncio
import contextlib
import io
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .audio_utils import AudioProcessingError, combine_wav_files, is_empty_audio, wav_duration_seconds
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
    audio_bytes: int = 0
    duration_seconds: float = 0.0


@dataclass(slots=True)
class VoiceEvent:
    timestamp: datetime
    event_type: str
    user_id: int | None
    display_name: str
    before_channel: str = ""
    after_channel: str = ""
    details: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(timespec="seconds"),
            "event_type": self.event_type,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "before_channel": self.before_channel,
            "after_channel": self.after_channel,
            "details": self.details,
        }

    def to_line(self) -> str:
        user = f"{self.display_name} ({self.user_id})" if self.user_id else self.display_name
        channels = ""
        if self.before_channel or self.after_channel:
            channels = f" [{self.before_channel or '-'} -> {self.after_channel or '-'}]"
        details = f" - {self.details}" if self.details else ""
        return f"[{self.timestamp.strftime('%H:%M:%S')}] {self.event_type}: {user}{channels}{details}"


@dataclass(slots=True)
class RecordedAudioBundle:
    session_id: str
    started_at: datetime
    stopped_at: datetime
    recording_dir: Path
    speaker_files: list[RecordedSpeakerFile] = field(default_factory=list)
    combined_raw_path: Path | None = None
    voice_events: list[VoiceEvent] = field(default_factory=list)
    crash_note: str | None = None


class PycordVoiceRecorder:
    def __init__(
        self,
        recordings_root: Path,
        audio_callback: Callable[[str, int, float], None] | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.recordings_root = recordings_root
        self.audio_callback = audio_callback
        self.event_callback = event_callback
        self.session_id: str | None = None
        self.started_at: datetime | None = None
        self.recording_dir: Path | None = None
        self.voice_events: list[VoiceEvent] = []
        self._sink: Any | None = None
        self._finished_future: asyncio.Future[Any] | None = None
        self._crash_error: str | None = None

    @property
    def is_recording(self) -> bool:
        return self._sink is not None and self._finished_future is not None and not self._finished_future.done()

    @property
    def has_unsaved_crash_data(self) -> bool:
        return self._finished_future is not None and self._finished_future.done()

    def total_audio_bytes(self) -> int:
        sink = self._sink
        if sink is None:
            return 0
        return sum(getattr(audio, "audio_bytes", 0) for audio in getattr(sink, "audio_data", {}).values())

    async def start(self, voice_client: Any) -> str:
        if self.is_recording:
            raise RecordingError("Recording is already in progress.")
        if self.has_unsaved_crash_data:
            raise RecordingError(
                "A previous recording ended unexpectedly and has not been saved yet. "
                "Press Stop to process it before starting a new one."
            )
        if voice_client is None or not getattr(voice_client, "is_connected", lambda: False)():
            raise RecordingError("Bot is not connected to a voice channel.")

        try:
            import discord
        except ImportError as exc:
            raise RecordingError("py-cord is not installed. Run python setup_nara.py first.") from exc

        self.session_id = utcish_now_id()
        self.started_at = datetime.now()
        self.recording_dir = ensure_dir(self.recordings_root / self.session_id)
        self.voice_events = []
        self._sink = create_nara_pcm_sink(discord, self.audio_callback)
        self._prepare_sink(self._sink, voice_client, discord)
        loop = asyncio.get_running_loop()
        self._finished_future = loop.create_future()
        sink = self._sink
        finished_future = self._finished_future

        def finished_callback(error: Exception | None) -> None:
            loop.call_soon_threadsafe(self._finish_recording, sink, finished_future, error)

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

    def add_voice_event(self, event: VoiceEvent) -> None:
        if self.is_recording:
            self.voice_events.append(event)

    @staticmethod
    def _prepare_sink(sink: Any, voice_client: Any, discord: Any) -> None:
        if hasattr(sink, "init"):
            sink.init(voice_client)
        if not hasattr(voice_client, "decoder"):
            voice_client.decoder = discord.opus.Decoder
        voice_client.recording = True

    def _finish_recording(self, sink: Any, finished_future: asyncio.Future[Any], error: Exception | None) -> None:
        if finished_future.done():
            return
        try:
            voice_client = getattr(sink, "vc", None)
            if voice_client is not None and not hasattr(voice_client, "recording"):
                setattr(voice_client, "recording", False)
            elif voice_client is not None:
                voice_client.recording = False
            sink.cleanup()
        except Exception as exc:
            finished_future.set_exception(RecordingError(f"Discord audio could not be finalized: {exc}"))
            return

        if error is not None:
            # The recording ended on its own (voice drop, DAVE decode failure, etc.) rather than
            # via an explicit stop() call. Keep the sink around so stop() can still salvage and
            # save whatever audio was captured, instead of silently losing the session.
            self._crash_error = str(error)
            if self.event_callback is not None:
                with contextlib.suppress(Exception):
                    self.event_callback(
                        "recording_crashed",
                        {"session_id": self.session_id, "error": str(error)},
                    )
        finished_future.set_result(sink)

    async def stop(self, voice_client: Any, bot: Any) -> RecordedAudioBundle:
        if self._finished_future is None:
            raise RecordingError("Recording has not started yet.")

        if not self._finished_future.done():
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
            crash_note = self._crash_error
            self._crash_error = None
            self._finished_future = None
            self._sink = None

        if not self.session_id or not self.started_at or not self.recording_dir:
            raise RecordingError("Recorder state was lost before files could be saved.")

        bundle = await self._save_sink_audio(sink, bot)
        if crash_note:
            bundle.crash_note = (
                f"Recording stopped early because of a Discord voice issue: {crash_note}. "
                "The audio captured up to that point was still processed."
            )
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
            if data[:4] == b"RIFF":
                raw_path.write_bytes(data)
            else:
                write_pcm_wav(raw_path, data)
            if is_empty_audio(raw_path):
                raw_path.unlink(missing_ok=True)
                continue
            duration_seconds = wav_duration_seconds(raw_path)
            audio_bytes = int(getattr(audio, "audio_bytes", 0) or len(data))
            speaker_files.append(
                RecordedSpeakerFile(
                    speaker=speaker,
                    user_id=user_id,
                    raw_path=raw_path,
                    speaker_id=speaker_id,
                    discord_display_name=discord_display_name,
                    audio_bytes=audio_bytes,
                    duration_seconds=duration_seconds,
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
            voice_events=list(self.voice_events),
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


class _PcmAudioData:
    def __init__(self) -> None:
        self.file = io.BytesIO()
        self.finished = False
        self.audio_bytes = 0
        self.first_offset: float | None = None
        self.last_offset: float | None = None

    def write(self, data: bytes, count_as_audio: bool = True, offset: float | None = None) -> None:
        if self.finished:
            return
        self.file.write(data)
        if count_as_audio:
            self.audio_bytes += len(data)
            if offset is not None and self.first_offset is None:
                self.first_offset = offset
            if offset is not None:
                self.last_offset = offset

    def cleanup(self) -> None:
        self.file.seek(0)
        self.finished = True


def create_nara_pcm_sink(
    discord: Any,
    audio_callback: Callable[[str, int, float], None] | None = None,
) -> Any:
    class NaraPcmSink(discord.sinks.Sink):
        __sink_listeners__: list[tuple[str, str]] = []
        encoding = "pcm"

        def __init__(self) -> None:
            super().__init__()
            self.audio_data: dict[int | str, _PcmAudioData] = {}
            self.started_at = time.perf_counter()
            self.bytes_per_second = 48000 * 2 * 2
            self._last_audio_report: dict[int | str, float] = {}

        @property
        def root(self) -> "NaraPcmSink":
            return self

        def is_opus(self) -> bool:
            return False

        def walk_children(self) -> tuple[Any, ...]:
            return ()

        def write(self, data: Any, user: Any) -> None:
            pcm = getattr(data, "pcm", data)
            if not isinstance(pcm, (bytes, bytearray)) or not pcm:
                return

            user_key = getattr(user, "id", None)
            if user_key is None and isinstance(user, (int, str)):
                user_key = user
            if user_key is None:
                source = getattr(data, "source", None)
                user_key = getattr(source, "id", None)
            if user_key is None:
                packet = getattr(data, "packet", None)
                ssrc = getattr(packet, "ssrc", None)
                user_key = getattr(getattr(self, "vc", None), "_ssrc_to_id", {}).get(ssrc)
            if user_key is None:
                return

            audio = self.audio_data.setdefault(user_key, _PcmAudioData())
            elapsed = max(0.0, time.perf_counter() - self.started_at)
            target_size = int(elapsed * self.bytes_per_second)
            current_size = audio.file.tell()
            if target_size > current_size:
                audio.write(b"\0" * (target_size - current_size), count_as_audio=False)
            audio.write(bytes(pcm), count_as_audio=True, offset=elapsed)

            if audio_callback is not None:
                last_report = self._last_audio_report.get(user_key, 0.0)
                if elapsed - last_report >= 1.0:
                    self._last_audio_report[user_key] = elapsed
                    audio_callback(str(user_key), audio.audio_bytes, elapsed)

        def cleanup(self) -> None:
            self.finished = True
            for audio in self.audio_data.values():
                audio.cleanup()

    return NaraPcmSink()


def write_pcm_wav(
    output_path: Path,
    pcm_data: bytes,
    *,
    channels: int = 2,
    sample_width: int = 2,
    frame_rate: int = 48000,
) -> None:
    frame_size = channels * sample_width
    usable_bytes = len(pcm_data) - (len(pcm_data) % frame_size)
    if usable_bytes <= 0:
        output_path.write_bytes(b"")
        return

    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(frame_rate)
        wav_file.writeframes(pcm_data[:usable_bytes])
