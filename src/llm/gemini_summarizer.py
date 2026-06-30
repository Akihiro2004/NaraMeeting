from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime


class GeminiError(RuntimeError):
    pass


CLEANUP_PROMPT = """You are cleaning a meeting transcript produced by local STT.

Clean this transcript while preserving the original meaning.

Rules:
- The transcript must only contain Indonesian and English.
- Do not translate everything into one language.
- Keep natural Indonesian-English code-switching.
- Remove or correct hallucinated non-Indonesian/non-English text.
- Keep technical terms in English when natural.
- Keep timestamps and speaker labels.
- Speaker labels can include Discord user IDs in parentheses; preserve those IDs.
- Do not add information that was not spoken.
- Do not invent names, decisions, deadlines, or action items.
- If a sentence is unclear, mark it as [unclear] instead of inventing content.
- Preserve the original meeting context as much as possible.
"""


SUMMARY_PROMPT = """You are Nara, an AI meeting assistant.

Create two outputs from this cleaned transcript:
1. A concise meeting summary
2. Formal meeting minutes / MoM

Important rules:
- Do not invent facts.
- Do not invent decisions.
- Do not invent attendees.
- Do not invent deadlines.
- Do not invent action item owners.
- If information is unclear, write "Not clearly mentioned."
- Preserve Indonesian-English mixed context naturally.
- Use Indonesian as the default language unless the transcript is mostly English.
- Extract action items only when they are actually implied or stated.
- Extract deadlines only when they are actually mentioned.

Return the answer exactly in this delimiter format:

---MEETING_SUMMARY_MD---
<markdown for meeting_summary.md>
---MEETING_MINUTES_MD---
<markdown for meeting_minutes.md>
"""


@dataclass(slots=True)
class SummaryResult:
    summary_markdown: str
    minutes_markdown: str
    raw_response: dict[str, str]


class GeminiSummarizer:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        if not api_key:
            raise GeminiError("Missing GEMINI_API_KEY. Please paste your Gemini API key into the .env file.")
        try:
            from google import genai
        except ImportError as exc:
            raise GeminiError("google-genai is not installed. Run python setup_nara.py first.") from exc

        self.model = model
        self.client = genai.Client(api_key=api_key)

    def clean_transcript(self, transcript_text: str) -> str:
        if not transcript_text.strip():
            raise GeminiError("Empty transcript. Record a meeting with audible speech, then try again.")
        prompt = f"{CLEANUP_PROMPT}\n\nTranscript:\n{transcript_text}"
        response_text = self._generate(prompt)
        cleaned = response_text.strip()
        if not cleaned:
            raise GeminiError("Gemini returned an empty cleaned transcript.")
        return cleaned

    def summarize(self, cleaned_transcript: str) -> SummaryResult:
        if not cleaned_transcript.strip():
            raise GeminiError("Empty cleaned transcript. Cannot create summary or meeting minutes.")
        response_text = self._generate(f"{SUMMARY_PROMPT}\n\nCleaned transcript:\n{cleaned_transcript}")
        summary, minutes = split_summary_response(response_text)
        raw_response = {
            "model": self.model,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "raw_text": response_text,
        }
        return SummaryResult(summary_markdown=summary, minutes_markdown=minutes, raw_response=raw_response)

    def _generate(self, prompt: str) -> str:
        try:
            response = self.client.models.generate_content(model=self.model, contents=prompt)
        except Exception as exc:  # pragma: no cover - network/API dependent
            raise GeminiError(
                "Gemini request failed. Check GEMINI_API_KEY, internet connection, and Gemini API access."
            ) from exc

        text = getattr(response, "text", None)
        if text:
            return text
        try:
            return json.dumps(response.model_dump(), ensure_ascii=False, indent=2)
        except Exception:
            return str(response)


def split_summary_response(response_text: str) -> tuple[str, str]:
    summary_marker = "---MEETING_SUMMARY_MD---"
    minutes_marker = "---MEETING_MINUTES_MD---"
    if summary_marker in response_text and minutes_marker in response_text:
        after_summary = response_text.split(summary_marker, 1)[1]
        summary, minutes = after_summary.split(minutes_marker, 1)
        return normalize_markdown(summary), normalize_markdown(minutes)

    fallback = normalize_markdown(response_text)
    return fallback, "## Meeting Minutes\n\nNot clearly mentioned.\n"


def normalize_markdown(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("markdown"):
            cleaned = cleaned[8:].strip()
    return cleaned.rstrip() + "\n"
