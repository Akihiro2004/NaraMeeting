from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

from .stt.base import AudioChunk
from .utils import ensure_dir


class AudioProcessingError(RuntimeError):
    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def require_ffmpeg() -> str:
    found = ffmpeg_path()
    if found:
        return found
    raise AudioProcessingError(
        "FFmpeg is missing. Install FFmpeg for Windows, add it to PATH, then run setup_nara.py again."
    )


def run_ffmpeg(args: list[str]) -> None:
    ffmpeg = require_ffmpeg()
    command = [ffmpeg, "-hide_banner", "-y", *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Unknown FFmpeg failure."
        raise AudioProcessingError(f"FFmpeg failed: {detail}")


def convert_to_16k_mono(input_path: Path, output_path: Path) -> Path:
    ensure_dir(output_path.parent)
    run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-af",
            "dynaudnorm=f=150:g=15",
            str(output_path),
        ]
    )
    if is_empty_audio(output_path):
        raise AudioProcessingError(f"Converted audio is empty or too short: {output_path}")
    return output_path


def combine_wav_files(input_paths: list[Path], output_path: Path) -> Path | None:
    existing = [path for path in input_paths if path.exists() and path.stat().st_size > 1024]
    if not existing:
        return None
    ensure_dir(output_path.parent)
    if len(existing) == 1:
        shutil.copy2(existing[0], output_path)
        return output_path

    args: list[str] = []
    for path in existing:
        args.extend(["-i", str(path)])
    args.extend(
        [
            "-filter_complex",
            f"amix=inputs={len(existing)}:duration=longest:dropout_transition=0,volume={len(existing)}",
            str(output_path),
        ]
    )
    run_ffmpeg(args)
    return output_path


def split_wav(input_path: Path, output_dir: Path, chunk_seconds: int, prefix: str = "chunk") -> list[AudioChunk]:
    ensure_dir(output_dir)
    duration = wav_duration_seconds(input_path)
    if duration <= 0.5:
        raise AudioProcessingError(f"Audio is empty or too short: {input_path}")

    chunks: list[AudioChunk] = []
    start = 0.0
    index = 1
    while start < duration:
        end = min(start + chunk_seconds, duration)
        chunk_path = output_dir / f"{prefix}_{index:04d}.wav"
        run_ffmpeg(
            [
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{end - start:.3f}",
                "-i",
                str(input_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                str(chunk_path),
            ]
        )
        if not is_empty_audio(chunk_path):
            chunks.append(AudioChunk(path=chunk_path, index=index, start_seconds=start, end_seconds=end))
        start += chunk_seconds
        index += 1

    if not chunks:
        raise AudioProcessingError(f"No usable audio chunks were created from {input_path}.")
    return chunks


def wav_duration_seconds(path: Path) -> float:
    if not path.exists() or path.stat().st_size < 512:
        return 0.0
    try:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
            if rate <= 0:
                return 0.0
            return frames / float(rate)
    except (wave.Error, OSError):
        return 0.0


def is_empty_audio(path: Path, min_seconds: float = 0.5, min_bytes: int = 2048) -> bool:
    if not path.exists() or path.stat().st_size < min_bytes:
        return True
    duration = wav_duration_seconds(path)
    return duration < min_seconds
