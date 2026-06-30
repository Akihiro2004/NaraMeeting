from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .utils import PROJECT_ROOT, parse_bool, parse_optional_int


class ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class NaraConfig:
    discord_token: str
    gemini_api_key: str
    default_voice_channel_id: int | None
    default_text_channel_id: int | None
    guild_id: int | None
    stt_engine: str
    stt_model_size: str
    stt_allowed_languages: list[str]
    stt_default_language: str
    stt_force_language_whitelist: bool
    stt_reject_other_languages: bool
    stt_chunk_seconds: int
    stt_compute_mode: str
    stt_threads: int
    output_language: str
    save_raw_audio: bool
    save_audio_chunks: bool
    contacts_file: Path
    obsidian_vault_path: Path
    obsidian_copy_audio: bool
    gemini_model: str
    project_root: Path = PROJECT_ROOT

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"

    @property
    def models_dir(self) -> Path:
        return self.project_root / "models" / "whisper"

    @property
    def recordings_dir(self) -> Path:
        return self.project_root / "data" / "recordings"

    @property
    def chunks_dir(self) -> Path:
        return self.project_root / "data" / "chunks"

    @property
    def transcripts_dir(self) -> Path:
        return self.project_root / "data" / "transcripts"

    @property
    def outputs_dir(self) -> Path:
        return self.project_root / "data" / "outputs"

    @property
    def contacts_dir(self) -> Path:
        return self.project_root / "data" / "contacts"


def ensure_env_file(project_root: Path = PROJECT_ROOT) -> Path:
    env_path = project_root / ".env"
    example_path = project_root / ".env.example"
    if not env_path.exists():
        if not example_path.exists():
            raise ConfigError(".env.example is missing. Restore it, then run setup_nara.py again.")
        shutil.copy2(example_path, env_path)
    return env_path


def load_config(require_secrets: bool = True, project_root: Path = PROJECT_ROOT) -> NaraConfig:
    env_path = ensure_env_file(project_root)
    values = dotenv_values(env_path)
    load_dotenv(env_path, override=True)

    discord_token = values.get("DISCORD_TOKEN", "").strip()
    gemini_api_key = values.get("GEMINI_API_KEY", "").strip()

    if require_secrets:
        if not discord_token:
            raise ConfigError("Missing DISCORD_TOKEN. Please paste your Discord bot token into the .env file.")
        if not gemini_api_key:
            raise ConfigError("Missing GEMINI_API_KEY. Please paste your Gemini API key into the .env file.")

    allowed_languages_raw = values.get("STT_ALLOWED_LANGUAGES", "id,en").replace(" ", "")
    allowed_languages = [item for item in allowed_languages_raw.split(",") if item]
    if allowed_languages != ["id", "en"]:
        raise ConfigError("Nara only supports Indonesian and English for this project.")

    model_size = values.get("STT_MODEL_SIZE", "tiny").strip().lower()
    if model_size not in {"tiny", "base"}:
        raise ConfigError("STT_MODEL_SIZE must be tiny or base for this lightweight local setup.")

    stt_engine = values.get("STT_ENGINE", "local_whisper").strip()
    if stt_engine != "local_whisper":
        raise ConfigError("Only STT_ENGINE=local_whisper is supported.")

    chunk_seconds = int(values.get("STT_CHUNK_SECONDS", "25") or "25")
    if chunk_seconds < 10 or chunk_seconds > 60:
        raise ConfigError("STT_CHUNK_SECONDS must be between 10 and 60 seconds.")

    threads = int(values.get("STT_THREADS", "4") or "4")
    if threads < 1:
        threads = 1

    obsidian_vault_raw = values.get("OBSIDIAN_VAULT_PATH", "obsidian_vault").strip() or "obsidian_vault"
    obsidian_vault_path = Path(obsidian_vault_raw)
    if not obsidian_vault_path.is_absolute():
        obsidian_vault_path = project_root / obsidian_vault_path

    contacts_raw = values.get("CONTACTS_FILE", "data/contacts/speaker_contacts.json").strip()
    contacts_file = Path(contacts_raw or "data/contacts/speaker_contacts.json")
    if not contacts_file.is_absolute():
        contacts_file = project_root / contacts_file

    return NaraConfig(
        discord_token=discord_token,
        gemini_api_key=gemini_api_key,
        default_voice_channel_id=parse_optional_int(values.get("DEFAULT_VOICE_CHANNEL_ID")),
        default_text_channel_id=parse_optional_int(values.get("DEFAULT_TEXT_CHANNEL_ID")),
        guild_id=parse_optional_int(values.get("GUILD_ID")),
        stt_engine=stt_engine,
        stt_model_size=model_size,
        stt_allowed_languages=allowed_languages,
        stt_default_language=values.get("STT_DEFAULT_LANGUAGE", "id").strip() or "id",
        stt_force_language_whitelist=parse_bool(values.get("STT_FORCE_LANGUAGE_WHITELIST"), True),
        stt_reject_other_languages=parse_bool(values.get("STT_REJECT_OTHER_LANGUAGES"), True),
        stt_chunk_seconds=chunk_seconds,
        stt_compute_mode=values.get("STT_COMPUTE_MODE", "cpu").strip() or "cpu",
        stt_threads=threads,
        output_language=values.get("OUTPUT_LANGUAGE", "mixed").strip() or "mixed",
        save_raw_audio=parse_bool(values.get("SAVE_RAW_AUDIO"), True),
        save_audio_chunks=parse_bool(values.get("SAVE_AUDIO_CHUNKS"), True),
        contacts_file=contacts_file,
        obsidian_vault_path=obsidian_vault_path,
        obsidian_copy_audio=parse_bool(values.get("OBSIDIAN_COPY_AUDIO"), False),
        gemini_model=values.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
        project_root=project_root,
    )


def dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def update_env_values(path: Path, updates: dict[str, str]) -> None:
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output_lines.append(line)
            continue
        key, _value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in updates:
            output_lines.append(f"{normalized_key}={updates[normalized_key]}")
            seen.add(normalized_key)
        else:
            output_lines.append(line)

    missing = [key for key in updates if key not in seen]
    if missing and output_lines and output_lines[-1].strip():
        output_lines.append("")
    for key in missing:
        output_lines.append(f"{key}={updates[key]}")

    path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
