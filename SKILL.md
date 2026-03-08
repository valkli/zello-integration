---
name: zello
description: Real-time voice push-to-talk integration with Zello Work. Connects to Zello channels, receives voice messages, processes them through STT → Claude LLM → TTS, and sends audio responses back.
metadata:
  openclaw:
    emoji: "📻"
    requires:
      bins: []
      env: []
---

# Zello PTT Integration Skill

Real-time voice push-to-talk communication with AI-powered responses through Zello Work channels.

## Architecture

```
Zello Channel → WebSocket → Opus Audio Stream
  → Decode Opus → PCM
  → ElevenLabs STT → Text
  → Claude Gateway → Response Text
  → ElevenLabs TTS → PCM Audio
  → Encode PCM → Opus
  → Zello Channel (voice response)
```

## Configuration

Create a `.env` file in the skill directory:

```bash
# Zello Work
ZELLO_WS_URL=wss://zellowork.io/ws/klincov
ZELLO_USERNAME=admin
ZELLO_PASSWORD=123Steha#
ZELLO_CHANNEL=Everyone

# ElevenLabs
ELEVENLABS_API_KEY=sk_3322952039182c89d0ed25e0b386e970f3994adb6e0f0e5e
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM

# Claude Gateway
CLAUDE_GATEWAY_URL=http://localhost:18789/v1
CLAUDE_GATEWAY_MODEL=anthropic/claude-haiku-4-5
CLAUDE_GATEWAY_TOKEN=  # Generate via: clawdbot gateway token --generate
```

## Usage

```bash
cd zello
python zello_skill.py
```

The skill will:
1. Connect to Zello Work WebSocket
2. Join the specified channel
3. Listen for voice messages
4. Process and respond with AI-generated voice

## Features

- **Real-time audio processing**: Opus 16kHz mono, 20ms packets
- **Speech-to-Text**: ElevenLabs Scribe API
- **AI Processing**: Claude via OpenClaw Gateway
- **Text-to-Speech**: ElevenLabs with streaming
- **Text message support**: Responds to text messages in channel

## Audio Format

- **Codec**: Opus
- **Sample Rate**: 16kHz
- **Channels**: Mono
- **Packet Size**: 20ms
