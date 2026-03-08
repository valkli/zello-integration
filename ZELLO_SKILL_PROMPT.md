# Zello PTT Integration Skill for OpenClaw - Complete Setup Prompt

## Goal
Create an OpenClaw skill that integrates with Zello Work for real-time voice push-to-talk communication with AI-powered responses. Zello connects to Gateway openclaw and works simply as a voice bridge.

## Credentials & Configuration

### Zello Work
- **Network:** klincov.zellowork.com
- **WebSocket:** wss://zellowork.io/ws/klincov
- **Username:** admin
- **Password:** 123Steha#
- **Channel:** Everyone
- **Audio:** Opus 16kHz mono, 20ms packets

### ElevenLabs
- **API Key:** sk_3322952039182c89d0ed25e0b386e970f3994adb6e0f0e5e
- **Voice ID:** 21m00Tcm4TlvDq8ikWAM (nova)
- **STT Endpoint:** https://api.elevenlabs.io/v1/speech-to-text
- **TTS Endpoint:** https://api.elevenlabs.io/v1/text-to-speech

### Claude Gateway
- **URL:** http://localhost:18789/v1
- **Model:** anthropic/claude-haiku-4-5
- **Token:** Generate via `clawdbot gateway token --generate`

### OpenClaw
https://github.com/openclaw/openclaw

### Official Zello API Documentation
- **GitHub:** https://github.com/zelloptt/zello-channel-api
- **API Spec:** https://github.com/zelloptt/zello-channel-api/blob/master/API.md
- **Python Examples:** https://github.com/zelloptt/zello-channel-api/tree/master/examples/py

## Skill Requirements

### Core Functionality
1. **WebSocket Connection to Zello Work** ✅ (WORKING)
   - Connect to wss://zellowork.io/ws/klincov
   - Authenticate with username/password
   - Receive on_channel_status events
   - Send text messages to channel

2. **Audio Stream Reception** 
   - Parse incoming Opus audio packets
   - Extract codec_header from on_stream_start
   - Buffer audio until on_stream_stop
   - Save raw Opus files

3. **Audio Processing Pipeline** 
   - Decode Opus → PCM
   - STT: Text conversion
   - LLM: Claude processing
   - TTS: Speech synthesis
   - Encode: PCM → Opus
   - Send audio back

4. **Text Message Support**
   - Parse on_text_message events
   - Send text responses