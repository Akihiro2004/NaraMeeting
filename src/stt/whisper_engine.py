from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .base import AudioChunk, TranscriptSegment


class STTError(RuntimeError):
    pass


_FOREIGN_SCRIPT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0600-\u06ff]+")
_REPEATED_WORD_RE = re.compile(r"\b(\w+)(?:\s+\1\b){4,}", re.IGNORECASE)


@dataclass(slots=True)
class CandidateSegment:
    start: float
    end: float
    language: str
    text: str
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float


@dataclass(slots=True)
class CandidateTranscript:
    language: str
    text: str
    segments: list[CandidateSegment]
    score: float


class LocalWhisperEngine:
    def __init__(
        self,
        model_size: str,
        models_dir: Path,
        compute_mode: str = "cpu",
        threads: int = 4,
        allowed_languages: list[str] | None = None,
    ) -> None:
        if model_size not in {"tiny", "base"}:
            raise STTError("Only tiny and base Whisper models are supported in this lightweight setup.")
        if compute_mode != "cpu":
            raise STTError("Nara is configured for CPU transcription only. Set STT_COMPUTE_MODE=cpu.")

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTError("faster-whisper is not installed. Run python setup_nara.py first.") from exc

        self.model_size = model_size
        self.models_dir = models_dir
        self.allowed_languages = allowed_languages or ["id", "en"]
        self.model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            cpu_threads=threads,
            download_root=str(models_dir),
        )

    def transcribe_chunks(
        self,
        chunks: Iterable[AudioChunk],
        speaker: str,
        speaker_id: str | None = None,
    ) -> list[TranscriptSegment]:
        output: list[TranscriptSegment] = []
        for chunk in chunks:
            candidates = [self._transcribe_for_language(chunk, language) for language in self.allowed_languages]
            best = self._choose_best(candidates)
            for segment in best.segments:
                text = clean_to_supported_scripts(segment.text)
                if not text:
                    continue
                output.append(
                    TranscriptSegment(
                        start=chunk.start_seconds + segment.start,
                        end=chunk.start_seconds + segment.end,
                        speaker=speaker,
                        language=best.language,
                        text=text,
                        speaker_id=speaker_id,
                    )
                )
        output.sort(key=lambda item: (item.start, item.speaker))
        return output

    def _transcribe_for_language(self, chunk: AudioChunk, language: str) -> CandidateTranscript:
        try:
            segments_iter, _info = self.model.transcribe(
                str(chunk.path),
                language=language,
                beam_size=1,
                best_of=1,
                vad_filter=True,
                condition_on_previous_text=False,
                temperature=0.0,
                word_timestamps=False,
            )
            segments = [
                CandidateSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    language=language,
                    text=(segment.text or "").strip(),
                    avg_logprob=float(getattr(segment, "avg_logprob", -5.0) or -5.0),
                    no_speech_prob=float(getattr(segment, "no_speech_prob", 1.0) or 1.0),
                    compression_ratio=float(getattr(segment, "compression_ratio", 10.0) or 10.0),
                )
                for segment in segments_iter
            ]
        except Exception as exc:  # pragma: no cover - depends on native runtime
            raise STTError(f"Local Whisper transcription failed for {chunk.path}: {exc}") from exc

        text = " ".join(segment.text for segment in segments).strip()
        return CandidateTranscript(language=language, text=text, segments=segments, score=score_candidate(text, segments, language))

    @staticmethod
    def _choose_best(candidates: list[CandidateTranscript]) -> CandidateTranscript:
        if not candidates:
            raise STTError("No STT candidates were generated.")
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        if len(candidates) >= 2 and abs(candidates[0].score - candidates[1].score) < 0.15:
            for candidate in candidates:
                if candidate.language == "id":
                    return candidate
        return candidates[0]


def score_candidate(text: str, segments: list[CandidateSegment], language: str) -> float:
    if not text:
        return -10.0

    score = 0.0
    avg_logprob_values = [segment.avg_logprob for segment in segments]
    no_speech_values = [segment.no_speech_prob for segment in segments]
    compression_values = [segment.compression_ratio for segment in segments]

    if avg_logprob_values:
        score += sum(avg_logprob_values) / len(avg_logprob_values)
    if no_speech_values:
        score -= sum(no_speech_values) / len(no_speech_values)
    if compression_values:
        compression = sum(compression_values) / len(compression_values)
        if compression > 2.4:
            score -= 1.5

    if _FOREIGN_SCRIPT_RE.search(text):
        score -= 4.0
    if _REPEATED_WORD_RE.search(text):
        score -= 2.0
    if len(text) < 3:
        score -= 1.0
    if language == "id":
        score += 0.05
    return score


def clean_to_supported_scripts(text: str) -> str:
    cleaned = _FOREIGN_SCRIPT_RE.sub("[unclear]", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if _REPEATED_WORD_RE.search(cleaned):
        return "[unclear]"
    return cleaned
