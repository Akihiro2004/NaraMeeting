from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def reexec_inside_venv_if_available() -> None:
    if os.environ.get("NARA_SKIP_VENV_REEXEC") == "1":
        return
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        return
    current = Path(sys.executable).resolve()
    target = venv_python.resolve()
    if str(current).lower() == str(target).lower():
        return
    env = os.environ.copy()
    env["NARA_SKIP_VENV_REEXEC"] = "1"
    completed = subprocess.run([str(target), *sys.argv], env=env)
    raise SystemExit(completed.returncode)


reexec_inside_venv_if_available()

from src.audio_utils import ffmpeg_path
from src.config import ConfigError, load_config
from src.logging_utils import setup_logging
from src.outputs import process_offline_stt


def main() -> int:
    parser = argparse.ArgumentParser(description="Nara Discord meeting recorder bot")
    parser.add_argument("--check", action="store_true", help="Validate local setup without starting Discord")
    parser.add_argument("--test-stt", type=Path, help="Run local STT on an audio file without Discord")
    parser.add_argument("--no-gui", action="store_true", help="Start without the channel picker GUI")
    parser.add_argument("--configure", action="store_true", help="Open the channel picker GUI and exit")
    args = parser.parse_args()

    logger = setup_logging(PROJECT_ROOT / "logs")

    if args.check:
        return run_check()

    try:
        config = load_config(require_secrets=False)
    except ConfigError as exc:
        print(exc)
        return 1

    if args.test_stt:
        try:
            result = process_offline_stt(args.test_stt, config, logger)
        except Exception as exc:
            logger.exception("Offline STT test failed")
            print(f"STT test failed: {exc}")
            return 1
        print("STT test finished.")
        print(f"Transcript: {result.files.transcript_clean}")
        print(f"Transcript JSON: {result.files.transcript_json}")
        return 0

    if not args.no_gui:
        try:
            from src.channel_picker import run_channel_picker
        except Exception as exc:
            print(f"Could not load channel picker GUI: {exc}")
            print("Run python main.py --no-gui to use the terminal/channel-ID flow.")
            return 1

        selection = run_channel_picker(PROJECT_ROOT, initial_token=config.discord_token)
        if selection is None:
            print("Nara was not started.")
            return 0

        config = load_config(require_secrets=False)
        config.discord_token = selection.token
        config.guild_id = selection.guild_id
        config.default_voice_channel_id = selection.voice_channel_id
        config.default_text_channel_id = selection.text_channel_id
        print(
            "Selected Discord target: "
            f"{selection.guild_name} / voice #{selection.voice_channel_name} / text #{selection.text_channel_name}"
        )
        if args.configure:
            print("Configuration saved. Start Nara with python main.py.")
            return 0
    elif args.configure:
        print("--configure requires the GUI. Run python main.py --configure without --no-gui.")
        return 1

    if not config.discord_token:
        print("Missing DISCORD_TOKEN. Use the GUI token field or paste your Discord bot token into the .env file.")
        return 1
    if not config.gemini_api_key:
        print("Missing GEMINI_API_KEY. Please paste your Gemini API key into the .env file.")
        return 1

    try:
        from src.bot import NaraBotRunner
    except Exception as exc:
        print(f"Could not load Discord bot runtime: {exc}")
        print("Run python setup_nara.py, then try python main.py again.")
        return 1

    try:
        import asyncio

        runner = NaraBotRunner(config, logger)
        asyncio.run(runner.run())
        return 0
    except ConfigError as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        print("Nara stopped.")
        return 0
    except Exception as exc:
        logger.exception("Nara crashed")
        print(f"Nara stopped because of an error: {exc}")
        return 1


def run_check() -> int:
    print("Nara setup check")
    ok = True

    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")

    try:
        config = load_config(require_secrets=False)
    except Exception as exc:
        print(f"Config check failed: {exc}")
        return 1

    env_path = PROJECT_ROOT / ".env"
    print(f".env exists: {'yes' if env_path.exists() else 'no'}")
    print(f"Speaker contacts file: {config.contacts_file}")

    if not config.discord_token:
        print("Missing DISCORD_TOKEN. Please paste your Discord bot token into the .env file.")
        ok = False
    if not config.gemini_api_key:
        print("Missing GEMINI_API_KEY. Please paste your Gemini API key into the .env file.")
        ok = False

    ffmpeg = ffmpeg_path()
    if ffmpeg:
        print(f"FFmpeg found: {ffmpeg}")
    else:
        print("FFmpeg missing. Install FFmpeg for Windows and add it to PATH.")
        ok = False

    for folder in [
        config.recordings_dir,
        config.chunks_dir,
        config.transcripts_dir,
        config.outputs_dir,
        config.contacts_dir,
        config.project_root / "data" / "runtime",
        config.logs_dir,
        config.models_dir,
        config.obsidian_vault_path,
    ]:
        folder.mkdir(parents=True, exist_ok=True)
        print(f"Folder exists: {folder}")

    dependency_modules = [
        "discord",
        "faster_whisper",
        "dotenv",
        "google.genai",
        "pydub",
        "aiofiles",
    ]
    for module in dependency_modules:
        try:
            importlib.import_module(module)
            print(f"Dependency import ok: {module}")
        except Exception as exc:
            print(f"Dependency import failed: {module} ({exc})")
            ok = False

    if whisper_model_available(config.models_dir, config.stt_model_size):
        print(f"STT model appears available: {config.stt_model_size}")
    else:
        print(
            f"STT model not found locally: {config.stt_model_size}. "
            "Run python setup_nara.py to download or prepare it."
        )
        ok = False

    print("Check complete." if ok else "Check finished with missing requirements.")
    return 0 if ok else 1


def whisper_model_available(models_dir: Path, model_size: str) -> bool:
    if not models_dir.exists():
        return False
    expected_names = [
        f"models--Systran--faster-whisper-{model_size}",
        f"faster-whisper-{model_size}",
        model_size,
    ]
    for path in models_dir.rglob("*"):
        if any(name in path.name for name in expected_names):
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
