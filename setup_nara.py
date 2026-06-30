from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
ENV_FILE = PROJECT_ROOT / ".env"
MODELS_DIR = PROJECT_ROOT / "models" / "whisper"


@dataclass(slots=True)
class PythonCandidate:
    command: list[str]
    version: tuple[int, int, int]
    executable: str

    @property
    def version_text(self) -> str:
        return f"Python {self.version[0]}.{self.version[1]}.{self.version[2]}"

    @property
    def reason(self) -> str:
        major_minor = self.version[:2]
        if major_minor == (3, 11):
            return "Python 3.11 is installed and is the preferred version for this project."
        if major_minor == (3, 10):
            return "Python 3.10 is installed and is the next preferred compatible version."
        if major_minor == (3, 12):
            return "Python 3.12 is installed and supported by the selected dependencies."
        return "This Python version is compatible."


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up Nara on Windows")
    parser.add_argument("--check", action="store_true", help="Check setup status without installing")
    args = parser.parse_args()

    if args.check:
        return run_check()
    return run_setup()


def run_setup() -> int:
    print("Nara setup started.")
    candidates = discover_python_versions()
    print_candidates(candidates)
    if select_python(candidates) is None:
        print("No suitable Python version found. Please install Python 3.11 or Python 3.10, then run setup_nara.py again.")
        return 1

    selected = create_venv_with_fallback(candidates)
    if selected is None:
        print("No suitable Python version could create a virtual environment.")
        print("Please install Python 3.11 or Python 3.10 from python.org, then run setup_nara.py again.")
        return 1

    print(f"Selected Python version: {selected.version_text}")
    print(f"Reason: {selected.reason}")

    install_dependencies()
    create_required_folders()
    ensure_env_file()
    check_ffmpeg()
    if not prepare_whisper_model("tiny"):
        return 1
    if not run_import_test():
        return 1

    print("")
    print("Nara setup finished successfully.")
    print("Next steps:")
    print("1. Open .env and paste DISCORD_TOKEN and GEMINI_API_KEY.")
    print("2. Run python main.py --check.")
    print("3. Run python main.py.")
    return 0


def run_check() -> int:
    print("Nara setup check")
    candidates = discover_python_versions()
    print_candidates(candidates)
    selected = select_python(candidates)
    if selected:
        print(f"Selected Python version: {selected.version_text}")
        print(f"Reason: {selected.reason}")
    else:
        print("No suitable Python version found. Please install Python 3.11 or Python 3.10, then run setup_nara.py again.")

    ok = selected is not None
    if VENV_PYTHON.exists():
        venv_version = python_version_for_command([str(VENV_PYTHON)])
        if venv_version:
            print(f"Existing Nara virtual environment: Python {venv_version[0]}.{venv_version[1]}.{venv_version[2]}")
    create_required_folders()
    ensure_env_file()

    env_values = read_env_values()
    if not env_values.get("DISCORD_TOKEN"):
        print("Missing DISCORD_TOKEN. Please paste your Discord bot token into the .env file.")
        ok = False
    if not env_values.get("GEMINI_API_KEY"):
        print("Missing GEMINI_API_KEY. Please paste your Gemini API key into the .env file.")
        ok = False
    if (env_values.get("STT_ALLOWED_LANGUAGES") or "id,en").replace(" ", "") != "id,en":
        print("Nara only supports Indonesian and English for this project.")
        ok = False

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        print(f"FFmpeg found: {ffmpeg}")
    else:
        print("FFmpeg missing.")
        print_ffmpeg_instructions()
        ok = False

    python_for_imports = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    if not run_import_test(python_for_imports, check_only=True):
        ok = False

    model_size = env_values.get("STT_MODEL_SIZE", "tiny") or "tiny"
    if whisper_model_available(model_size):
        print(f"STT model appears available: {model_size}")
    else:
        print(f"STT model missing: {model_size}. Run python setup_nara.py to download it.")
        ok = False

    print("Check complete." if ok else "Check finished with missing requirements.")
    return 0 if ok else 1


def discover_python_versions() -> list[PythonCandidate]:
    commands = [
        ["py", "-3.11"],
        ["py", "-3.10"],
        ["py", "-3.12"],
    ]
    commands.extend(commands_from_where("python3"))
    commands.extend(commands_from_where("python"))
    commands.extend([["python3"], ["python"]])
    candidates: list[PythonCandidate] = []
    seen: set[tuple[str, tuple[int, int]]] = set()

    for command in commands:
        version = python_version_for_command(command)
        if version is None:
            continue
        executable = python_executable_for_command(command) or " ".join(command)
        key = (executable.lower(), version[:2])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(PythonCandidate(command=command, version=version, executable=executable))
    return candidates


def commands_from_where(executable_name: str) -> list[list[str]]:
    try:
        result = subprocess.run(["where.exe", executable_name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    commands: list[list[str]] = []
    for raw_line in result.stdout.splitlines():
        path = raw_line.strip()
        normalized = path.lower().replace("/", "\\")
        if "\\.venv\\" in normalized or "\\venv\\" in normalized:
            continue
        if path:
            commands.append([path])
    return commands


def python_version_for_command(command: list[str]) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout + result.stderr).strip()
    match = re.search(r"Python\s+(\d+)\.(\d+)\.(\d+)", output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def python_executable_for_command(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            [*command, "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def select_python(candidates: list[PythonCandidate]) -> PythonCandidate | None:
    supported = sort_supported_pythons(candidates)
    return supported[0] if supported else None


def sort_supported_pythons(candidates: list[PythonCandidate]) -> list[PythonCandidate]:
    priority = {(3, 11): 0, (3, 10): 1, (3, 12): 2}
    supported = [candidate for candidate in candidates if candidate.version[:2] in priority]
    supported.sort(key=lambda candidate: priority[candidate.version[:2]])
    return supported


def print_candidates(candidates: list[PythonCandidate]) -> None:
    if not candidates:
        print("Installed Python versions found: none")
        return
    print("Installed Python versions found:")
    for candidate in candidates:
        print(f"- {candidate.version_text}: {candidate.executable}")


def create_venv_with_fallback(candidates: list[PythonCandidate]) -> PythonCandidate | None:
    if VENV_PYTHON.exists():
        version = python_version_for_command([str(VENV_PYTHON)])
        if version and version[:2] in {(3, 10), (3, 11), (3, 12)}:
            print(f"Virtual environment already exists: {VENV_DIR}")
            return PythonCandidate(command=[str(VENV_PYTHON)], version=version, executable=str(VENV_PYTHON))
    if VENV_DIR.exists() and not VENV_PYTHON.exists():
        remove_partial_venv()

    for candidate in sort_supported_pythons(candidates):
        print(f"Trying Python for virtual environment: {candidate.version_text} ({candidate.executable})")
        if create_venv(candidate):
            return candidate
        remove_partial_venv()
    return None


def create_venv(selected: PythonCandidate) -> bool:
    if VENV_PYTHON.exists():
        print(f"Virtual environment already exists: {VENV_DIR}")
        return True
    print(f"Creating virtual environment: {VENV_DIR}")
    result = subprocess.run([*selected.command, "-m", "venv", str(VENV_DIR)], capture_output=True, text=True)
    if result.returncode != 0 or not VENV_PYTHON.exists():
        detail = (result.stderr or result.stdout).strip()
        if detail:
            print(f"Venv creation failed detail: {detail.splitlines()[-1]}")
        print(f"Could not create .venv with {selected.version_text}. Trying the next compatible Python if available.")
        return False
    print("Virtual environment created.")
    return True


def remove_partial_venv() -> None:
    if not VENV_DIR.exists():
        return
    root = PROJECT_ROOT.resolve()
    target = VENV_DIR.resolve()
    if root != target and root in target.parents:
        shutil.rmtree(target, ignore_errors=True)


def install_dependencies() -> None:
    print("Installing dependencies inside .venv.")
    run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    print("Dependencies installed.")


def create_required_folders() -> None:
    folders = [
        PROJECT_ROOT / "models" / "whisper",
        PROJECT_ROOT / "data" / "recordings",
        PROJECT_ROOT / "data" / "chunks",
        PROJECT_ROOT / "data" / "transcripts",
        PROJECT_ROOT / "data" / "outputs",
        PROJECT_ROOT / "data" / "contacts",
        PROJECT_ROOT / "data" / "runtime",
        PROJECT_ROOT / "logs",
        PROJECT_ROOT / "obsidian_vault",
    ]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    contacts_file = PROJECT_ROOT / "data" / "contacts" / "speaker_contacts.json"
    if not contacts_file.exists():
        contacts_file.write_text('{\n  "version": 1,\n  "updated_at": "",\n  "users": {}\n}\n', encoding="utf-8")
    print("Required folders are ready.")


def ensure_env_file() -> None:
    if ENV_FILE.exists():
        print(".env already exists. It was not overwritten.")
        return
    if not ENV_EXAMPLE.exists():
        raise RuntimeError(".env.example is missing.")
    shutil.copy2(ENV_EXAMPLE, ENV_FILE)
    print(".env created from .env.example.")


def check_ffmpeg() -> bool:
    found = shutil.which("ffmpeg")
    if found:
        print(f"FFmpeg found: {found}")
        return True
    print("FFmpeg is missing.")
    print_ffmpeg_instructions()
    return False


def print_ffmpeg_instructions() -> None:
    print("Windows FFmpeg install options:")
    print("1. winget install Gyan.FFmpeg")
    print("2. Close and reopen PowerShell.")
    print("3. Run ffmpeg -version to confirm it is on PATH.")


def prepare_whisper_model(model_size: str) -> bool:
    if whisper_model_available(model_size):
        print(f"STT model already appears available: {model_size}")
        return True
    print(f"Downloading/preparing faster-whisper model: {model_size}")
    code = (
        "from faster_whisper import WhisperModel\n"
        f"WhisperModel({model_size!r}, device='cpu', compute_type='int8', download_root={str(MODELS_DIR)!r})\n"
        "print('Model ready')\n"
    )
    result = subprocess.run([str(VENV_PYTHON), "-c", code], capture_output=True, text=True)
    if result.returncode == 0 and whisper_model_available(model_size):
        print(f"STT model ready: {model_size}")
        return True

    print("Whisper model download failed.")
    detail = (result.stderr or result.stdout).strip()
    if detail:
        print(f"Failure detail: {detail.splitlines()[-1]}")
    print(f"Model needed: faster-whisper {model_size} multilingual")
    print(f"Place or download it under: {MODELS_DIR}")
    print("Then rerun: python setup_nara.py")
    return False


def whisper_model_available(model_size: str) -> bool:
    expected_names = [
        f"models--Systran--faster-whisper-{model_size}",
        f"faster-whisper-{model_size}",
        model_size,
    ]
    if not MODELS_DIR.exists():
        return False
    for path in MODELS_DIR.rglob("*"):
        if any(name in path.name for name in expected_names):
            return True
    return False


def run_import_test(python_executable: Path = VENV_PYTHON, check_only: bool = False) -> bool:
    print("Running dependency import test.")
    code = (
        "import discord\n"
        "import faster_whisper\n"
        "from dotenv import load_dotenv\n"
        "from google import genai\n"
        "import pydub\n"
        "import aiofiles\n"
        "print('Import test passed')\n"
    )
    if not python_executable.exists():
        print(f"Python executable not found for import test: {python_executable}")
        return False
    result = subprocess.run([str(python_executable), "-c", code], capture_output=True, text=True)
    if result.returncode == 0:
        print("Dependency import test passed.")
        return True
    print("Dependency import test failed.")
    detail = (result.stderr or result.stdout).strip()
    if detail:
        print(f"Failure detail: {detail.splitlines()[-1]}")
    if not check_only:
        print("Run python setup_nara.py again after checking the pip install error above.")
    return False


def read_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def run(command: list[str]) -> None:
    print(f"> {' '.join(command)}")
    result = subprocess.run(command)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
