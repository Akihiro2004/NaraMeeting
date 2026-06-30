from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .audio_utils import convert_to_16k_mono, split_wav
from .config import NaraConfig
from .contacts import SpeakerContactBook
from .llm.gemini_summarizer import GeminiSummarizer
from .recorder import RecordedAudioBundle, RecordedSpeakerFile
from .stt.base import TranscriptSegment, seconds_to_timestamp
from .stt.whisper_engine import LocalWhisperEngine
from .utils import atomic_write_text, copy_if_exists, ensure_dir, iso_now, safe_filename, utcish_now_id, write_json


class OutputError(RuntimeError):
    pass


@dataclass(slots=True)
class GeneratedFiles:
    transcript_raw: Path
    transcript_clean: Path
    transcript_json: Path
    speaker_contacts_json: Path | None = None
    meeting_summary: Path | None = None
    meeting_minutes: Path | None = None
    gemini_response_json: Path | None = None
    obsidian_meeting_note: Path | None = None

    def discord_uploads(self, include_summary: bool) -> list[Path]:
        files = [self.transcript_clean]
        if include_summary:
            if self.meeting_summary:
                files.insert(0, self.meeting_summary)
            if self.meeting_minutes:
                files.insert(1, self.meeting_minutes)
        return [path for path in files if path and path.exists()]


@dataclass(slots=True)
class ProcessingResult:
    session_id: str
    transcript_dir: Path
    output_dir: Path
    chunks_dir: Path
    recording_dir: Path
    files: GeneratedFiles
    segments: list[TranscriptSegment] = field(default_factory=list)


@dataclass(slots=True)
class ConvertedSpeakerAudio:
    speaker: str
    speaker_id: str | None
    discord_display_name: str
    source_raw_path: Path
    converted_path: Path


def process_recording_bundle(
    bundle: RecordedAudioBundle,
    config: NaraConfig,
    logger,
    summarize: bool = True,
) -> ProcessingResult:
    logger.info("Audio conversion started")
    transcript_dir = ensure_dir(config.transcripts_dir / bundle.session_id)
    output_dir = ensure_dir(config.outputs_dir / bundle.session_id)
    chunks_dir = ensure_dir(config.chunks_dir / bundle.session_id)
    contact_book = SpeakerContactBook(config.contacts_file).load()

    converted = convert_speaker_files(bundle.speaker_files, contact_book, logger)
    if not converted:
        raise OutputError("No speaker audio files were available for transcription.")

    if bundle.combined_raw_path:
        try:
            convert_to_16k_mono(bundle.combined_raw_path, bundle.recording_dir / "combined_16k_mono.wav")
        except Exception as exc:
            logger.warning("Could not create combined_16k_mono.wav: %s", exc)

    logger.info("Audio chunking started")
    engine = LocalWhisperEngine(
        model_size=config.stt_model_size,
        models_dir=config.models_dir,
        compute_mode=config.stt_compute_mode,
        threads=config.stt_threads,
        allowed_languages=config.stt_allowed_languages,
    )
    logger.info("STT model loaded: %s", config.stt_model_size)

    all_segments: list[TranscriptSegment] = []
    for item in converted:
        speaker_dir = chunks_dir / safe_filename(f"user_{item.speaker_id}" if item.speaker_id else item.speaker)
        chunks = split_wav(item.converted_path, speaker_dir, config.stt_chunk_seconds)
        logger.info("Transcribing %s from %s chunks", item.speaker, len(chunks))
        all_segments.extend(engine.transcribe_chunks(chunks, item.speaker, speaker_id=item.speaker_id))

    all_segments.sort(key=lambda segment: (segment.start, segment.speaker))
    if not all_segments:
        raise OutputError("Empty transcript. Record a meeting with audible Indonesian or English speech.")

    logger.info("Transcribing finished")
    raw_transcript = render_transcript(all_segments)
    transcript_raw = transcript_dir / "transcript_raw.txt"
    transcript_clean = transcript_dir / "transcript_clean.txt"
    transcript_json = transcript_dir / "transcript.json"
    speaker_contacts_json = transcript_dir / "speaker_contacts.json"
    atomic_write_text(transcript_raw, raw_transcript)
    write_json(transcript_json, [segment.to_json() for segment in all_segments])
    write_meeting_speaker_contacts(speaker_contacts_json, converted, contact_book)

    summarizer = GeminiSummarizer(config.gemini_api_key, model=config.gemini_model)
    logger.info("Gemini cleanup started")
    cleaned_transcript = summarizer.clean_transcript(raw_transcript)
    atomic_write_text(transcript_clean, cleaned_transcript)
    logger.info("Gemini cleanup finished")

    meeting_summary: Path | None = None
    meeting_minutes: Path | None = None
    gemini_response_json: Path | None = None
    if summarize:
        logger.info("Gemini summary started")
        summary = summarizer.summarize(cleaned_transcript)
        meeting_summary = output_dir / "meeting_summary.md"
        meeting_minutes = output_dir / "meeting_minutes.md"
        gemini_response_json = output_dir / "gemini_response.json"
        atomic_write_text(meeting_summary, summary.summary_markdown)
        atomic_write_text(meeting_minutes, summary.minutes_markdown)
        write_json(gemini_response_json, summary.raw_response)
        logger.info("Gemini summary finished")

    generated = GeneratedFiles(
        transcript_raw=transcript_raw,
        transcript_clean=transcript_clean,
        transcript_json=transcript_json,
        speaker_contacts_json=speaker_contacts_json,
        meeting_summary=meeting_summary,
        meeting_minutes=meeting_minutes,
        gemini_response_json=gemini_response_json,
    )
    logger.info("Output files saved")

    vault_writer = ObsidianVaultWriter(
        config.obsidian_vault_path,
        copy_audio=config.obsidian_copy_audio,
        contact_book=contact_book,
    )
    generated.obsidian_meeting_note = vault_writer.write_meeting(
        bundle=bundle,
        generated=generated,
        segments=all_segments,
    )
    logger.info("Obsidian vault updated: %s", generated.obsidian_meeting_note)

    return ProcessingResult(
        session_id=bundle.session_id,
        transcript_dir=transcript_dir,
        output_dir=output_dir,
        chunks_dir=chunks_dir,
        recording_dir=bundle.recording_dir,
        files=generated,
        segments=all_segments,
    )


def process_offline_stt(audio_path: Path, config: NaraConfig, logger) -> ProcessingResult:
    if not audio_path.exists():
        raise OutputError(f"Audio file not found: {audio_path}")

    session_id = f"test_{utcish_now_id()}"
    recording_dir = ensure_dir(config.recordings_dir / session_id)
    transcript_dir = ensure_dir(config.transcripts_dir / session_id)
    output_dir = ensure_dir(config.outputs_dir / session_id)
    chunks_dir = ensure_dir(config.chunks_dir / session_id)

    copied_audio = recording_dir / f"{safe_filename(audio_path.stem)}_raw{audio_path.suffix}"
    shutil.copy2(audio_path, copied_audio)
    converted_audio = convert_to_16k_mono(copied_audio, recording_dir / "Speaker_16k_mono.wav")
    chunks = split_wav(converted_audio, chunks_dir / "Speaker", config.stt_chunk_seconds)

    engine = LocalWhisperEngine(
        model_size=config.stt_model_size,
        models_dir=config.models_dir,
        compute_mode=config.stt_compute_mode,
        threads=config.stt_threads,
        allowed_languages=config.stt_allowed_languages,
    )
    segments = engine.transcribe_chunks(chunks, "Speaker")
    if not segments:
        raise OutputError("No transcript segments were generated from the test audio.")

    raw_transcript = render_transcript(segments)
    transcript_raw = transcript_dir / "transcript_raw.txt"
    transcript_clean = transcript_dir / "transcript_clean.txt"
    transcript_json = transcript_dir / "transcript.json"
    atomic_write_text(transcript_raw, raw_transcript)
    atomic_write_text(transcript_clean, raw_transcript)
    write_json(transcript_json, [segment.to_json() for segment in segments])

    return ProcessingResult(
        session_id=session_id,
        transcript_dir=transcript_dir,
        output_dir=output_dir,
        chunks_dir=chunks_dir,
        recording_dir=recording_dir,
        files=GeneratedFiles(
            transcript_raw=transcript_raw,
            transcript_clean=transcript_clean,
            transcript_json=transcript_json,
            speaker_contacts_json=None,
        ),
        segments=segments,
    )


def convert_speaker_files(
    speaker_files: list[RecordedSpeakerFile],
    contact_book: SpeakerContactBook,
    logger,
) -> list[ConvertedSpeakerAudio]:
    converted: list[ConvertedSpeakerAudio] = []
    for speaker_file in speaker_files:
        contact = contact_book.upsert_seen(speaker_file.speaker_id, speaker_file.discord_display_name or speaker_file.speaker)
        speaker_label = contact.transcript_label if contact else speaker_file.speaker
        safe_speaker = safe_filename(f"user_{speaker_file.speaker_id}" if speaker_file.speaker_id else speaker_label)
        output_path = speaker_file.raw_path.with_name(f"{safe_speaker}_16k_mono.wav")
        logger.info("Audio converted: %s", output_path)
        converted_path = convert_to_16k_mono(speaker_file.raw_path, output_path)
        converted.append(
            ConvertedSpeakerAudio(
                speaker=speaker_label,
                speaker_id=speaker_file.speaker_id,
                discord_display_name=speaker_file.discord_display_name,
                source_raw_path=speaker_file.raw_path,
                converted_path=converted_path,
            )
        )
    return converted


def render_transcript(segments: list[TranscriptSegment]) -> str:
    lines: list[str] = []
    for segment in segments:
        lines.append(
            f"[{seconds_to_timestamp(segment.start)} - {seconds_to_timestamp(segment.end)}] "
            f"[{segment.speaker}] [{segment.language}] {segment.text}"
        )
    return "\n".join(lines).rstrip() + "\n"


def write_meeting_speaker_contacts(
    path: Path,
    speakers: list[ConvertedSpeakerAudio],
    contact_book: SpeakerContactBook,
) -> None:
    rows: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for speaker in speakers:
        key = speaker.speaker_id or speaker.speaker
        if key in seen:
            continue
        seen.add(key)
        contact = contact_book.get(speaker.speaker_id)
        rows.append(
            {
                "speaker_id": speaker.speaker_id,
                "transcript_label": speaker.speaker,
                "discord_display_name": speaker.discord_display_name or None,
                "preferred_name": contact.preferred_name if contact else None,
            }
        )
    write_json(path, rows)


class ObsidianVaultWriter:
    def __init__(
        self,
        vault_root: Path,
        copy_audio: bool = False,
        contact_book: SpeakerContactBook | None = None,
    ) -> None:
        self.vault_root = vault_root
        self.copy_audio = copy_audio
        self.contact_book = contact_book

    def write_meeting(
        self,
        bundle: RecordedAudioBundle,
        generated: GeneratedFiles,
        segments: list[TranscriptSegment],
    ) -> Path:
        self._ensure_vault()
        meeting_dt = bundle.started_at
        date_text = meeting_dt.strftime("%Y-%m-%d")
        time_text = meeting_dt.strftime("%H:%M")
        month_text = meeting_dt.strftime("%Y-%m")
        title = f"{date_text} {meeting_dt.strftime('%H-%M')} Nara Meeting"
        meeting_dir = ensure_dir(self.vault_root / "Meetings" / meeting_dt.strftime("%Y") / month_text)

        transcript_note = meeting_dir / f"{title} Transcript.md"
        summary_note = meeting_dir / f"{title} Summary.md"
        minutes_note = meeting_dir / f"{title} Minutes.md"
        meeting_note = meeting_dir / f"{title}.md"
        attachments_dir = ensure_dir(self.vault_root / "Attachments" / "Nara" / bundle.session_id)

        copy_if_exists(generated.transcript_clean, attachments_dir / "transcript_clean.txt")
        copy_if_exists(generated.transcript_json, attachments_dir / "transcript.json")
        if generated.meeting_summary:
            copy_if_exists(generated.meeting_summary, attachments_dir / "meeting_summary.md")
        if generated.meeting_minutes:
            copy_if_exists(generated.meeting_minutes, attachments_dir / "meeting_minutes.md")
        if generated.gemini_response_json:
            copy_if_exists(generated.gemini_response_json, attachments_dir / "gemini_response.json")
        if generated.speaker_contacts_json:
            copy_if_exists(generated.speaker_contacts_json, attachments_dir / "speaker_contacts.json")
        if self.copy_audio:
            for speaker_file in bundle.speaker_files:
                copy_if_exists(speaker_file.raw_path, attachments_dir / speaker_file.raw_path.name)
            if bundle.combined_raw_path:
                copy_if_exists(bundle.combined_raw_path, attachments_dir / bundle.combined_raw_path.name)

        speaker_records = self._speaker_records(segments)
        attendees = [record["label"] for record in speaker_records]
        languages = sorted({segment.language for segment in segments if segment.language})
        transcript_link = self._wikilink(transcript_note, "Transcript")
        summary_link = self._wikilink(summary_note, "Summary") if generated.meeting_summary else "Not generated."
        minutes_link = self._wikilink(minutes_note, "Minutes") if generated.meeting_minutes else "Not generated."

        transcript_body = self._frontmatter(
            {
                "type": "transcript",
                "session_id": bundle.session_id,
                "date": date_text,
                "meeting": self._link_target(meeting_note),
                "tags": ["nara/transcript", "meeting/transcript"],
            }
        )
        transcript_body += "## Transcript\n\n"
        transcript_body += generated.transcript_clean.read_text(encoding="utf-8")
        atomic_write_text(transcript_note, transcript_body)

        if generated.meeting_summary:
            body = self._frontmatter(
                {
                    "type": "meeting-summary",
                    "session_id": bundle.session_id,
                    "date": date_text,
                    "meeting": self._link_target(meeting_note),
                    "tags": ["nara/summary", "meeting/summary"],
                }
            )
            body += generated.meeting_summary.read_text(encoding="utf-8")
            atomic_write_text(summary_note, body)

        if generated.meeting_minutes:
            body = self._frontmatter(
                {
                    "type": "meeting-minutes",
                    "session_id": bundle.session_id,
                    "date": date_text,
                    "meeting": self._link_target(meeting_note),
                    "tags": ["nara/minutes", "meeting/minutes"],
                }
            )
            body += generated.meeting_minutes.read_text(encoding="utf-8")
            atomic_write_text(minutes_note, body)

        meeting_body = self._frontmatter(
            {
                "type": "meeting",
                "session_id": bundle.session_id,
                "date": date_text,
                "time": time_text,
                "started": bundle.started_at.isoformat(timespec="seconds"),
                "stopped": bundle.stopped_at.isoformat(timespec="seconds"),
                "attendees": attendees,
                "discord_user_ids": [record["speaker_id"] for record in speaker_records if record["speaker_id"]],
                "languages": languages,
                "tags": ["nara/meeting", "meeting"],
            }
        )
        meeting_body += "## Context\n\n"
        meeting_body += f"- Session: `{bundle.session_id}`\n"
        meeting_body += f"- Local recording folder: `{bundle.recording_dir}`\n"
        meeting_body += f"- Transcript: {transcript_link}\n"
        meeting_body += f"- Summary: {summary_link}\n"
        meeting_body += f"- Minutes: {minutes_link}\n"
        meeting_body += "\n## Attendees\n\n"
        if speaker_records:
            for record in speaker_records:
                person_note = self._ensure_person_note_for_speaker(record["speaker_id"], record["label"])
                meeting_body += f"- {self._wikilink(person_note, record['label'])}\n"
        else:
            meeting_body += "- Not clearly mentioned.\n"
        meeting_body += "\n## Results\n\n"
        if generated.meeting_summary:
            meeting_body += generated.meeting_summary.read_text(encoding="utf-8").strip() + "\n"
        else:
            meeting_body += "- Summary was not requested for this run.\n"
        meeting_body += "\n## Files\n\n"
        meeting_body += f"- {transcript_link}\n"
        if generated.meeting_summary:
            meeting_body += f"- {summary_link}\n"
        if generated.meeting_minutes:
            meeting_body += f"- {minutes_link}\n"
        meeting_body += f"- Attachments folder: [[{self._link_target(attachments_dir)}]]\n"
        atomic_write_text(meeting_note, meeting_body)

        index_line = f"- {self._wikilink(meeting_note, title)} - {date_text} {time_text} - {', '.join(attendees) or 'Speaker'}"
        self._append_under_header(self.vault_root / "Indexes" / "Nara Meetings.md", "## Meetings", index_line)

        daily_line = f"- {time_text} {self._wikilink(meeting_note, 'Nara Meeting')} - transcript {transcript_link}"
        self._append_under_header(self.vault_root / "Daily" / f"{date_text}.md", "## Meetings", daily_line)

        log_line = (
            f"- {datetime.now().strftime('%H:%M')} processed {self._wikilink(meeting_note, title)} "
            f"from session `{bundle.session_id}`; transcript {transcript_link}."
        )
        self._append_under_header(self.vault_root / "Logs" / "Nara Processing Log.md", f"## {date_text}", log_line)
        self.sync_contacts_index()
        return meeting_note

    def _ensure_vault(self) -> None:
        ensure_dir(self.vault_root)
        for folder in [
            ".obsidian",
            "Meetings",
            "Daily",
            "People",
            "Contacts",
            "Attachments",
            "Templates",
            "Indexes",
            "Logs",
        ]:
            ensure_dir(self.vault_root / folder)
        app_json = self.vault_root / ".obsidian" / "app.json"
        if not app_json.exists():
            atomic_write_text(app_json, '{\n  "alwaysUpdateLinks": true,\n  "newFileLocation": "folder",\n  "newFileFolderPath": "Inbox"\n}\n')
        index_path = self.vault_root / "Indexes" / "Nara Meetings.md"
        if not index_path.exists():
            atomic_write_text(
                index_path,
                self._frontmatter({"type": "index", "tags": ["nara/index", "meetings"]})
                + "## Meetings\n\n",
            )
        template_path = self.vault_root / "Templates" / "Nara Meeting Note.md"
        if not template_path.exists():
            atomic_write_text(
                template_path,
                "---\ntype: meeting\nsession_id:\ndate:\ntags:\n  - nara/meeting\n  - meeting\n---\n\n"
                "## Context\n\n## Attendees\n\n## Results\n\n## Files\n",
            )
        contacts_index = self.vault_root / "Contacts" / "Discord Users.md"
        if not contacts_index.exists():
            atomic_write_text(
                contacts_index,
                self._frontmatter({"type": "contacts-index", "tags": ["nara/contacts", "discord/users"]})
                + "## Contacts\n\n",
            )

    def sync_contacts_index(self) -> None:
        self._ensure_vault()
        if self.contact_book is None:
            return
        contacts = self.contact_book.all_contacts()
        lines = [
            self._frontmatter({"type": "contacts-index", "tags": ["nara/contacts", "discord/users"]}).rstrip(),
            "## Contacts",
            "",
        ]
        if not contacts:
            lines.append("- No Discord speaker contacts captured yet.")
        for contact in contacts:
            person_note = self._ensure_person_note_for_speaker(contact.discord_user_id, contact.transcript_label)
            preferred = contact.preferred_name or "Not set"
            discord_name = contact.discord_display_name or "Unknown"
            relationship = f" - relationship: {contact.relationship}" if contact.relationship else ""
            lines.append(
                f"- {self._wikilink(person_note, contact.transcript_label)} - ID `{contact.discord_user_id}` - "
                f"preferred: {preferred} - Discord: {discord_name}{relationship}"
            )
        atomic_write_text(self.vault_root / "Contacts" / "Discord Users.md", "\n".join(lines).rstrip() + "\n")

    def _ensure_person_note_for_speaker(self, speaker_id: str | None, label: str) -> Path:
        if speaker_id:
            path = self.vault_root / "People" / f"Discord User {speaker_id}.md"
        else:
            path = self.vault_root / "People" / f"{safe_filename(label)}.md"
        if path.exists():
            return path

        contact = self.contact_book.get(speaker_id) if self.contact_book and speaker_id else None
        body = self._frontmatter(
            {
                "type": "person",
                "name": contact.best_name if contact else label,
                "discord_user_id": speaker_id or "",
                "preferred_name": contact.preferred_name if contact else "",
                "discord_display_name": contact.discord_display_name if contact else "",
                "relationship": contact.relationship if contact else "",
                "aliases": contact.aliases if contact else [],
                "tags": ["person", "nara/person", "discord/user"],
            }
        )
        body += "## Profile\n\n"
        body += f"- Discord user ID: `{speaker_id or 'unknown'}`\n"
        body += f"- Current label: {label}\n"
        if contact:
            body += f"- Preferred name: {contact.preferred_name or 'Not set'}\n"
            body += f"- Discord display name: {contact.discord_display_name or 'Unknown'}\n"
            body += f"- Relationship: {contact.relationship or 'Not set'}\n"
            if contact.aliases:
                body += f"- Aliases: {', '.join(contact.aliases)}\n"
        body += "\n## Meetings\n\n"
        atomic_write_text(path, body)
        return path

    def _speaker_records(self, segments: list[TranscriptSegment]) -> list[dict[str, str | None]]:
        records: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for segment in segments:
            key = segment.speaker_id or segment.speaker
            if not key or key in seen:
                continue
            seen.add(key)
            label = segment.speaker
            if self.contact_book and segment.speaker_id:
                contact = self.contact_book.get(segment.speaker_id)
                if contact:
                    label = contact.transcript_label
            records.append({"speaker_id": segment.speaker_id, "label": label})
        return records

    def _append_under_header(self, path: Path, header: str, line: str) -> None:
        if path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            text = self._frontmatter({"type": "log", "created": iso_now(), "tags": ["nara/log"]}) + f"{header}\n\n"
        if line in text:
            return
        if header not in text:
            if not text.endswith("\n"):
                text += "\n"
            text += f"\n{header}\n\n"
        lines = text.splitlines()
        header_index = lines.index(header)
        insert_at = header_index + 1
        while insert_at < len(lines) and (lines[insert_at].strip() == "" or lines[insert_at].startswith("- ")):
            insert_at += 1
        lines.insert(insert_at, line)
        atomic_write_text(path, "\n".join(lines).rstrip() + "\n")

    def _frontmatter(self, values: dict[str, object]) -> str:
        lines = ["---"]
        for key, value in values.items():
            lines.extend(self._yaml_value(key, value))
        lines.append("---")
        lines.append("")
        return "\n".join(lines)

    def _yaml_value(self, key: str, value: object) -> list[str]:
        if isinstance(value, list):
            lines = [f"{key}:"]
            if not value:
                lines.append("  []")
            else:
                for item in value:
                    lines.append(f"  - {self._yaml_scalar(item)}")
            return lines
        return [f"{key}: {self._yaml_scalar(value)}"]

    @staticmethod
    def _yaml_scalar(value: object) -> str:
        text = str(value).replace('"', '\\"')
        if text == "" or any(char in text for char in [":", "#", "[", "]", "{", "}", ",", "`"]):
            return f'"{text}"'
        return text

    def _wikilink(self, path: Path, alias: str | None = None) -> str:
        target = self._link_target(path)
        if alias:
            return f"[[{target}|{alias}]]"
        return f"[[{target}]]"

    def _link_target(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.vault_root)
        except ValueError:
            relative = path
        if relative.suffix == ".md":
            relative = relative.with_suffix("")
        return str(relative).replace("\\", "/")
