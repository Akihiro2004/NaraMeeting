# Nara

Nara is a lightweight Discord meeting recorder bot for Windows. It joins a Discord voice channel by channel ID, records the meeting, transcribes audio locally with faster-whisper, cleans the transcript with Gemini, and writes summary outputs plus an Obsidian-compatible vault.

Nara is designed for Indonesian and English meetings, including natural mixed Indonesian-English speech. Raw audio stays local. Gemini only receives transcript text.

## What Nara Creates

For each meeting, Nara saves:

- Raw and converted audio in `data/recordings/YYYY-MM-DD_HH-MM-SS/`
- Audio chunks in `data/chunks/YYYY-MM-DD_HH-MM-SS/`
- `transcript_raw.txt`, `transcript_clean.txt`, and `transcript.json` in `data/transcripts/YYYY-MM-DD_HH-MM-SS/`
- `speaker_contacts.json` in each transcript folder as the per-meeting speaker ID snapshot
- `meeting_summary.md`, `meeting_minutes.md`, and `gemini_response.json` in `data/outputs/YYYY-MM-DD_HH-MM-SS/`
- Obsidian notes in `obsidian_vault/`
- Persistent speaker contacts in `data/contacts/speaker_contacts.json`

## Obsidian Vault Format

An Obsidian vault is just a local folder of Markdown files. Nara uses that model directly:

```text
obsidian_vault/
├─ .obsidian/
├─ Meetings/
│  └─ YYYY/
│     └─ YYYY-MM/
│        ├─ YYYY-MM-DD HH-mm Nara Meeting.md
│        ├─ YYYY-MM-DD HH-mm Nara Meeting Transcript.md
│        ├─ YYYY-MM-DD HH-mm Nara Meeting Summary.md
│        └─ YYYY-MM-DD HH-mm Nara Meeting Minutes.md
├─ Daily/
│  └─ YYYY-MM-DD.md
├─ People/
├─ Contacts/
│  └─ Discord Users.md
├─ Attachments/
│  └─ Nara/
│     └─ session_id/
├─ Templates/
├─ Indexes/
│  └─ Nara Meetings.md
└─ Logs/
   └─ Nara Processing Log.md
```

Meeting notes use YAML properties, tags, and `[[wikilinks]]`. Nara automatically updates the meeting index, daily note, attendee notes, Discord contact index, and processing log.

## Speaker IDs and Contact Names

When Discord voice receive provides per-user audio, Nara keeps the Discord user ID through the whole pipeline:

- Raw files are saved with stable names such as `user_123456789_raw.wav`.
- Transcript lines use labels such as `Darrien (123456789)` or `Discord User 123456789`.
- `transcript.json` includes `speaker_id` for each segment.
- `data/contacts/speaker_contacts.json` stores known speaker IDs.
- `obsidian_vault/Contacts/Discord Users.md` links those IDs to `People/Discord User <id>.md`.

If the bot can see the Discord display name, it saves that automatically. You can assign a cleaner preferred name later:

```text
/set_speaker_name user_id name
```

Example:

```text
/set_speaker_name 123456789 Darrien
```

Future transcripts will use `Darrien (123456789)`. Existing transcript JSON still keeps the raw `speaker_id`, so old meetings stay traceable.

## System Requirements

- Windows 10 or Windows 11
- Python 3.10, 3.11, or 3.12
- FFmpeg on PATH
- A Discord bot token
- A Gemini API key
- A CPU is enough. GPU is not required.

Recommended Python priority:

1. Python 3.11
2. Python 3.10
3. Python 3.12

The setup script checks installed Python commands first. It uses 3.11 if available, then 3.10, then 3.12.

## Install

Run:

```powershell
python setup_nara.py
```

The setup script will:

- Check installed Python versions
- Create `.venv`
- Install Python dependencies inside `.venv`
- Create `.env` from `.env.example` if missing
- Create required folders
- Check FFmpeg
- Download or prepare the tiny multilingual faster-whisper model
- Run dependency import checks

After setup, open `.env` and paste:

```env
DISCORD_TOKEN=your_discord_bot_token
GEMINI_API_KEY=your_gemini_api_key
```

Do not paste keys into Python files.

## FFmpeg on Windows

Recommended:

```powershell
winget install Gyan.FFmpeg
```

Then close and reopen PowerShell and verify:

```powershell
ffmpeg -version
```

## Create the Discord Bot

1. Open the Discord Developer Portal.
2. Create an application.
3. Add a bot.
4. Copy the bot token into `.env` as `DISCORD_TOKEN`.
5. Enable required intents if your server policy requires them:
   - Server Members Intent is optional for Nara.
   - Message Content Intent is not required for slash commands.
6. Invite the bot with these permissions:
   - View Channels
   - Use Slash Commands
   - Send Messages
   - Attach Files
   - Connect
   - Speak

The bot must be in the target server before channel IDs can work.

### Nara Invite URL

Use this URL when inviting Nara to another Discord server:

```text
https://discord.com/oauth2/authorize?client_id=1521394581151944794&permissions=2150665216&integration_type=0&scope=bot%20applications.commands
```

This URL includes the required scopes:

- `bot`
- `applications.commands`

And the selected bot permissions integer:

```text
2150665216
```

## Run

Validate setup:

```powershell
python main.py --check
```

Start Nara:

```powershell
python main.py
```

If `.venv` exists, `main.py` automatically reruns itself inside `.venv`, so the command still works from a normal PowerShell window.

Nara opens a compact channel picker GUI first. It connects with the bot token, shows servers the bot is already in, then lets you select:

- Discord server
- Voice channel to join
- Text channel for result files

Click **Start Nara** after selecting channels. You do not need to copy channel IDs manually.

To open only the GUI and save the last selected channels without starting the bot:

```powershell
python main.py --configure
```

To skip the GUI and use the older terminal/channel-ID flow:

```powershell
python main.py --no-gui
```

`DEFAULT_VOICE_CHANNEL_ID`, `DEFAULT_TEXT_CHANNEL_ID`, and `GUILD_ID` can stay blank in `.env` when using the GUI.

## Channel Picker GUI

The GUI uses the bot token from `.env`. If the token is blank, paste it into the masked token field and click **Connect**.

The picker saves:

- `DISCORD_TOKEN` back into `.env` if you entered it in the GUI
- Last selected guild, voice channel, and text channel in `data/runtime/last_channel_selection.json`

The selected channels are passed to the bot for the current run. Nara still shows the picker on normal startup so you can change servers/channels without editing `.env`.

## Slash Commands

```text
/join voice_channel_id
/start_record
/stop_record summarize:true
/leave
/status
/set_output_channel text_channel_id
/set_speaker_name user_id name
/list_speakers
/help_nara
```

Use `/start_record` only after everyone knows the meeting is being recorded. Nara posts:

```text
Nara is now recording this voice channel. Please make sure everyone in the meeting is aware.
```

Use `/list_speakers` after a meeting to see captured Discord user IDs. Use `/set_speaker_name` to assign a preferred name to one of those IDs.

## STT Model

Default:

```env
STT_MODEL_SIZE=tiny
```

Use `tiny` for modest laptops. For better accuracy, change:

```env
STT_MODEL_SIZE=base
```

Then run:

```powershell
python setup_nara.py
```

Nara uses CPU mode by default:

```env
STT_COMPUTE_MODE=cpu
STT_THREADS=4
```

## Offline STT Test

You can test transcription without Discord:

```powershell
python main.py --test-stt path\to\audio.wav
```

This converts audio, chunks it, transcribes locally, and saves transcript files. It does not require Discord or Gemini keys.

## Language Rules

Nara only supports:

- Indonesian: `id`
- English: `en`

Required setting:

```env
STT_ALLOWED_LANGUAGES=id,en
```

Any other language setting is rejected.

## Privacy

- Get consent before recording any meeting.
- Nara announces recording when `/start_record` runs.
- Raw audio is never uploaded to Gemini or any STT API.
- Local STT runs on your computer through faster-whisper.
- Gemini receives transcript text only.
- `.env` is ignored by git.

## Common Errors

`Missing DISCORD_TOKEN`

Paste the bot token into `.env`.

`Missing GEMINI_API_KEY`

Paste the Gemini API key into `.env`.

`FFmpeg is missing`

Install FFmpeg and reopen PowerShell.

`Voice channel not found`

Use the numeric Discord voice channel ID, not the channel name.

`Missing Discord voice permissions`

Grant the bot Connect and Speak in that voice channel.

`No usable audio was captured`

Make sure users spoke, the bot was connected, and the Discord voice receive API worked for that channel.

`Transcript only shows Discord User <id>`

That is expected when no preferred name is set yet. Run `/list_speakers`, then `/set_speaker_name user_id name`.

`Dependency import failed`

Run `python setup_nara.py` again and inspect the pip error.

## Discord Recording Caveat

Discord voice recording from Python depends on library support and Discord voice behavior. Nara uses Pycord's recording sink API and validates captured audio after `/stop_record`. If Discord or permissions prevent receiving audio, Nara stops and reports the issue instead of creating fake transcript files.

## Quick Test Meeting

1. Start Nara with `python main.py`.
2. Join a short voice call.
3. Run `/join voice_channel_id`.
4. Run `/start_record`.
5. Speak for 20 to 30 seconds in Indonesian, English, or both.
6. Run `/stop_record summarize:true`.
7. Check Discord output and `obsidian_vault/Indexes/Nara Meetings.md`.
8. Run `/list_speakers`, then `/set_speaker_name user_id name` for any captured speaker IDs.
