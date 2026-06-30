from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Protocol


@dataclass(slots=True)
class AudioChunk:
    path: Path
    index: int
    start_seconds: float
    end_seconds: float


@dataclass(slots=True)
class TranscriptSegment:
    start: float
    end: float
    speaker: str
    language: str
    text: str
    speaker_id: str | None = None

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["start"] = seconds_to_timestamp(self.start)
        data["end"] = seconds_to_timestamp(self.end)
        return data


class STTEngine(Protocol):
    def transcribe_chunks(
        self,
        chunks: Iterable[AudioChunk],
        speaker: str,
        speaker_id: str | None = None,
    ) -> list[TranscriptSegment]:
        ...


def seconds_to_timestamp(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
