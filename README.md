# Zello Integration

> Real-time voice notification and push-to-talk integration with Zello Work for AI agent pipelines.

## Overview

An AI-powered agent skill that connects to Zello push-to-talk channels and delivers voice notifications from automated pipelines — publishing confirmations, reminders, status updates — spoken aloud in real time.

## Features

- **Voice notifications** — text-to-speech via ElevenLabs, sent as Zello audio messages
- **Queue-based architecture** — agents write to `notify_queue.json`; skill reads and sends through existing WebSocket connection (avoids connection conflicts)
- **Auto-reconnect** — exponential backoff reconnection (5–60s) on connection loss
- **Watchdog** — Windows Task Scheduler watchdog restarts the process if it dies
- **Opus audio** — native Zello audio codec (OPUS 8kHz, Zello proprietary framing)
- **Command listener** — responds to voice/text commands in the Zello channel

## Architecture

```
AI Agent → notify_queue.json → zello_skill.py (persistent WS connection)
                                              → ElevenLabs TTS
                                              → Opus encode
                                              → Zello WebSocket → Channel
```

## Requirements

- Python 3.10+
- ElevenLabs API Key (`ELEVENLABS_API_KEY`)
- Zello account credentials (`ZELLO_USERNAME`, `ZELLO_PASSWORD`, `ZELLO_AUTH_TOKEN`)
- `opus.dll` / `ffmpeg.dll` (Windows) for audio encoding

## Environment Variables

```
ZELLO_USERNAME=your_zello_username
ZELLO_PASSWORD=your_zello_password
ZELLO_AUTH_TOKEN=your_zello_jwt_token
ELEVENLABS_API_KEY=your_elevenlabs_key
```

## Usage

```bash
# Install dependencies
pip install -r requirements.txt

# Start Zello skill (persistent connection)
python zello_skill.py

# Send a test notification
python zello_commands.py "Hello from the AI agent"

# Test connection
python test_connection.py
```

## Notification Protocol

Other agent components send notifications by writing to `notify_queue.json`:

```python
import json
from pathlib import Path

queue = Path("path/to/notify_queue.json")
messages = json.loads(queue.read_text()) if queue.exists() else []
messages.append("Publication complete: Product Name, 45€")
queue.write_text(json.dumps(messages))
```

The skill reads the queue every 5 seconds and delivers messages as voice audio.

## Agent Skill

This is an **OpenClaw agent skill** — part of a multi-agent automation pipeline providing real-time voice feedback for headless operations.

---

*Integrates with: Milanuncios Auto-Publisher, Wallapop Auto-Publisher, Twitter Campaign.*
