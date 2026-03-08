#!/usr/bin/env python3
"""
Zello PTT Integration Skill for OpenClaw
========================================
Connects to Zello Work, receives voice messages, processes through STT → LLM → TTS,
and sends audio responses back to the channel.
"""

import asyncio
import base64
import json
import logging
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx
# -----------------------------------------------------------------------------
# БЛОК ИНИЦИАЛИЗАЦИИ DLL (Windows Fix)
# -----------------------------------------------------------------------------
# Пытаемся принудительно загрузить opus.dll для Windows
OPUS_AVAILABLE = False
opuslib = None

try:
    import ctypes
    from ctypes.util import find_library
    
    # Пытаемся найти библиотеку стандартными средствами
    lib_name = find_library('opus')
    
    # Если не найдено (частая проблема Windows), пытаемся загрузить из текущей папки
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Проверяем несколько возможных мест для opus.dll
    # Примечание: папка Zello больше не нужна, opus.dll должен быть в System32 или в текущей директории
    possible_paths = [
        os.path.join(script_dir, 'opus.dll'),
        os.path.join(os.getcwd(), 'opus.dll'),
        os.path.join(script_dir, 'libopus-0.dll'),
        os.path.join(script_dir, 'libopus.dll'),
        # Папка Zello больше не используется - opus.dll должен быть в System32
        # os.path.join(script_dir, 'Zello', 'opus.dll'),  # Отключено - не используется
    ]
    
    dll_path = None
    for path in possible_paths:
        if os.path.exists(path):
            dll_path = path
            break
    
    if dll_path:
        try:
            # Принудительная загрузка DLL перед импортом opuslib
            # Используем абсолютный путь
            abs_dll_path = os.path.abspath(dll_path)
            dll_dir = os.path.dirname(abs_dll_path)
            
            # Добавляем директорию в PATH ПЕРЕД загрузкой DLL
            # Это критично для opuslib, который ищет библиотеку через find_library
            if dll_dir not in os.environ.get('PATH', ''):
                os.environ['PATH'] = dll_dir + os.pathsep + os.environ.get('PATH', '')
            
            # Загружаем DLL
            ctypes.CDLL(abs_dll_path)
            print(f"✅ Загружен opus.dll: {abs_dll_path}", file=sys.stderr)
            print(f"✅ Директория добавлена в PATH: {dll_dir}", file=sys.stderr)
            
            # opuslib использует find_library, который ищет в System32
            # Пробуем скопировать в System32 для opuslib ПЕРЕД импортом opuslib
            import shutil
            system32_dll = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'System32', 'opus.dll')
            if not os.path.exists(system32_dll):
                try:
                    shutil.copy2(abs_dll_path, system32_dll)
                    print(f"✅ Скопирован opus.dll в System32: {system32_dll}", file=sys.stderr)
                except PermissionError:
                    print(f"⚠️ Нет прав для копирования в System32. Запустите скрипт от имени администратора или скопируйте opus.dll в System32 вручную.", file=sys.stderr)
                except Exception as e:
                    print(f"⚠️ Не удалось скопировать в System32: {e}", file=sys.stderr)
            else:
                print(f"✅ opus.dll уже в System32", file=sys.stderr)
            
            # Теперь импортируем opuslib ПОСЛЕ копирования в System32
            import opuslib
            OPUS_AVAILABLE = True
            print("✅ opuslib успешно импортирован", file=sys.stderr)
        except Exception as e:
            print(f"Ошибка загрузки opus.dll: {e}", file=sys.stderr)
            print(f"Попробовали загрузить: {abs_dll_path}", file=sys.stderr)
            OPUS_AVAILABLE = False
            opuslib = None
    else:
        print("opus.dll не найден в следующих местах:", file=sys.stderr)
        for path in possible_paths:
            print(f"  - {path}", file=sys.stderr)
        print("Декодирование через opuslib не будет работать.", file=sys.stderr)
        print("Инструкция: скачайте libopus-0.dll, переименуйте в opus.dll и положите в текущую директорию.", file=sys.stderr)
        OPUS_AVAILABLE = False
        opuslib = None
    
    # Если opuslib не был импортирован выше, пробуем импортировать здесь
    if OPUS_AVAILABLE is False:
        try:
            import opuslib
            OPUS_AVAILABLE = True
            print("✅ opuslib успешно импортирован (fallback)", file=sys.stderr)
        except (ImportError, Exception) as e:
            OPUS_AVAILABLE = False
            opuslib = None
            print(f"opuslib недоступен: {e}", file=sys.stderr)
except (ImportError, Exception) as e:
    OPUS_AVAILABLE = False
    opuslib = None
    print(f"opuslib недоступен (outer exception): {e}", file=sys.stderr)

try:
    import ogg
    OGG_AVAILABLE = True
except (ImportError, Exception):
    OGG_AVAILABLE = False
    ogg = None

try:
    import pyogg
    PYOGG_AVAILABLE = True
except (ImportError, Exception) as e:
    PYOGG_AVAILABLE = False
    pyogg = None

# Use ffmpeg directly via subprocess (pydub has issues with Python 3.14)
import shutil
FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg"))
PYDUB_AVAILABLE = False  # Disabled due to Python 3.14 audioop issue

import re
import websockets
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

ZELLO_WS_URL = os.getenv("ZELLO_WS_URL", "wss://zello.io/ws")
ZELLO_USERNAME = os.getenv("ZELLO_USERNAME", "valeryklintsou")
ZELLO_PASSWORD = os.getenv("ZELLO_PASSWORD", "")
ZELLO_AUTH_TOKEN = os.getenv("ZELLO_AUTH_TOKEN", "")  # JWT token for consumer Zello
ZELLO_CHANNEL = os.getenv("ZELLO_CHANNEL", "ali-assistant")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"

CLAUDE_GATEWAY_URL = os.getenv("CLAUDE_GATEWAY_URL", "http://localhost:18789/v1")
CLAUDE_GATEWAY_MODEL = os.getenv("CLAUDE_GATEWAY_MODEL", "anthropic/claude-haiku-4-5")
CLAUDE_GATEWAY_TOKEN = os.getenv("CLAUDE_GATEWAY_TOKEN", "")

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SIZE = 320  # 20ms at 16kHz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("zello-skill")

if not OPUS_AVAILABLE and not FFMPEG_AVAILABLE:
    log.warning(
        "No Opus support available. Install one of:\n"
        "  - opuslib (requires Opus library): pip install opuslib\n"
        "  - pydub with ffmpeg: pip install pydub (and install ffmpeg)"
    )
elif not OPUS_AVAILABLE and FFMPEG_AVAILABLE:
    log.info("Using ffmpeg for Opus support (opuslib not available)")

# ---------------------------------------------------------------------------
# Opus Codec
# ---------------------------------------------------------------------------

class OpusCodec:
    """Opus encoder/decoder for 16kHz mono audio."""
    
    def __init__(self):
        self.decoder = None
        self.encoder = None
        self.codec_header = None
        self.use_pydub = not OPUS_AVAILABLE and PYDUB_AVAILABLE
        
    def init_decoder(self, codec_header: bytes):
        """Initialize decoder with codec header from Zello.
        
        codec_header structure (4 bytes, Little Endian):
        - Bytes 0-1: Sample Rate (uint16, Little Endian)
        - Byte 2: Frames Per Packet (uint8)
        - Byte 3: Frame Duration in ms (uint8)
        """
        self.codec_header = codec_header
        
        # Parse codec_header to get sample rate and frame duration
        if len(codec_header) >= 4:
            # Sample Rate (bytes 0-1, Little Endian)
            sample_rate = struct.unpack('<H', codec_header[0:2])[0]
            # Frame Duration (byte 3)
            frame_duration = codec_header[3]
            
            log.info(f"Codec header parsed: sample_rate={sample_rate} Hz, frame_duration={frame_duration} ms")
            
            # Update global SAMPLE_RATE if different (though usually 16000)
            global SAMPLE_RATE
            if sample_rate != SAMPLE_RATE:
                log.warning(f"Sample rate mismatch: codec_header={sample_rate}, expected={SAMPLE_RATE}")
                # Use codec_header value for this stream
                self.sample_rate = sample_rate
            else:
                self.sample_rate = SAMPLE_RATE
            
            self.frame_duration = frame_duration
        else:
            log.warning(f"Invalid codec_header length: {len(codec_header)}, using defaults")
            self.sample_rate = SAMPLE_RATE
            self.frame_duration = 60  # Default 60ms
        
        if OPUS_AVAILABLE:
            # Use opuslib
            try:
                self.decoder = opuslib.Decoder(self.sample_rate, CHANNELS)
                log.info(f"Opus decoder initialized (opuslib): {self.sample_rate} Hz, {CHANNELS} channel(s)")
            except Exception as e:
                log.error(f"Failed to initialize Opus decoder: {e}")
                raise
        elif FFMPEG_AVAILABLE:
            # Use ffmpeg directly - no initialization needed, just mark as available
            log.info(f"Using ffmpeg for Opus decoding (no initialization needed): {self.sample_rate} Hz")
            self.decoder = "ffmpeg"  # Mark as available
        else:
            # Debug: check what's available
            log.error(f"FFMPEG_AVAILABLE={FFMPEG_AVAILABLE}, OPUS_AVAILABLE={OPUS_AVAILABLE}, PYDUB_AVAILABLE={PYDUB_AVAILABLE}")
            raise RuntimeError(
                "No Opus decoder available. Install opuslib (requires Opus library) "
                "or ensure ffmpeg is in PATH"
            )
        
    def init_encoder(self, stereo=False, sample_rate=None):
        """Initialize encoder for sending audio back.
        
        Args:
            stereo: If True, encode in stereo (2 channels). If False, use CHANNELS setting.
            sample_rate: Sample rate in Hz. If None, uses SAMPLE_RATE (16000).
        """
        if OPUS_AVAILABLE:
            # Use opuslib
            try:
                channels = 2 if stereo else CHANNELS
                encode_rate = sample_rate if sample_rate is not None else SAMPLE_RATE
                # According to technical report: Config 11 (Wideband 16kHz) is correct for 16kHz
                # But incoming stream is Config 0/3 (8kHz), so try 8kHz
                # Try APPLICATION_VOIP (optimized for voice) - may work better than APPLICATION_AUDIO
                self.encoder = opuslib.Encoder(encode_rate, channels, opuslib.APPLICATION_VOIP)
                # According to technical report: typical range 16-32 kbps for speech
                # Try 24 kbps for better quality (was 16kbps, didn't work)
                self.encoder.bitrate = 24000
                log.info(f"Opus encoder initialized (opuslib): {encode_rate} Hz, {channels} channel(s), bitrate={self.encoder.bitrate}")
            except Exception as e:
                log.error(f"Failed to initialize Opus encoder: {e}")
                raise
        elif PYDUB_AVAILABLE:
            # Use pydub with ffmpeg
            log.info("Using pydub/ffmpeg for Opus encoding")
        else:
            raise RuntimeError(
                "No Opus encoder available. Install opuslib (requires Opus library) "
                "or pydub with ffmpeg: pip install pydub"
            )
        
    def decode(self, opus_data: bytes, frame_size: Optional[int] = None) -> bytes:
        """Decode Opus packet to PCM.
        
        Args:
            opus_data: Raw Opus packet data (starts with TOC byte)
            frame_size: Expected frame size in samples. If None, calculated from frame_duration.
        
        According to technical report:
        - TOC 0x18 = Config 3 (SILK Narrowband, 60ms)
        - Frame size for 60ms at 16kHz = 960 samples
        - Must use at least 960 samples for 60ms frames
        """
        if OPUS_AVAILABLE and self.decoder:
            try:
                # Calculate frame size if not provided
                if frame_size is None:
                    # frame_duration is in ms, sample_rate is in Hz
                    # frame_size = (sample_rate * frame_duration) / 1000
                    frame_size = int(self.sample_rate * self.frame_duration / 1000)
                
                # According to technical report: for 60ms frames at 16kHz, need at least 960 samples
                # If calculated size is less, use 960 as minimum
                if frame_size < 960:
                    frame_size = 960
                
                # opuslib.Decoder.decode expects: (data, frame_size)
                pcm = self.decoder.decode(opus_data, frame_size=frame_size)
                return pcm
            except opuslib.OpusError as e:
                # If packet was shorter (e.g., 20ms), try smaller size
                # or this is a corrupted packet
                if frame_size >= 960:
                    try:
                        pcm = self.decoder.decode(opus_data, frame_size=320)
                        return pcm
                    except:
                        log.error(f"Opus decode error (fallback): {e}")
                        return b""
                else:
                    log.error(f"Opus decode error: {e}")
                    return b""
            except Exception as e:
                log.error(f"Opus decode error: {e}")
                return b""
        elif PYDUB_AVAILABLE:
            # Use pydub to decode Opus
            try:
                # Save to temp file and decode
                import tempfile
                import os
                with tempfile.NamedTemporaryFile(suffix='.opus', delete=False) as f:
                    f.write(opus_data)
                    temp_opus = f.name
                
                try:
                    audio = AudioSegment.from_file(temp_opus, format="opus")
                    # Convert to 16kHz mono PCM
                    audio = audio.set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS)
                    pcm = audio.raw_data
                    return pcm
                finally:
                    os.unlink(temp_opus)
            except Exception as e:
                log.error(f"Pydub decode error: {e}")
                return b""
        else:
            raise RuntimeError("Decoder not initialized")
            
    def encode(self, pcm_data: bytes, frame_size: Optional[int] = None) -> bytes:
        """Encode PCM to Opus packet.
        
        Args:
            pcm_data: PCM audio data (16-bit samples)
            frame_size: Frame size in samples. If None, uses FRAME_SIZE (320 = 20ms).
                        For Zello, should use 960 samples (60ms at 16kHz).
        """
        if OPUS_AVAILABLE and self.encoder:
            try:
                # Use provided frame_size or default to FRAME_SIZE
                if frame_size is None:
                    frame_size = FRAME_SIZE
                opus = self.encoder.encode(pcm_data, frame_size=frame_size)
                return opus
            except Exception as e:
                log.error(f"Opus encode error: {e}")
                return b""
        elif PYDUB_AVAILABLE:
            # Use pydub to encode to Opus
            # Note: pydub creates a full Opus file, not individual packets
            # For Zello we need to send packets, so this is a workaround
            try:
                # Create AudioSegment from PCM
                audio = AudioSegment(
                    pcm_data,
                    frame_rate=SAMPLE_RATE,
                    channels=CHANNELS,
                    sample_width=2  # 16-bit
                )
                # Export to Opus
                import tempfile
                import os
                with tempfile.NamedTemporaryFile(suffix='.opus', delete=False) as f:
                    temp_opus = f.name
                
                try:
                    audio.export(temp_opus, format="opus", bitrate="16k", parameters=["-frame_duration", "20"])
                    with open(temp_opus, "rb") as f:
                        opus_file = f.read()
                    # Extract Opus packets from file (skip Ogg container header)
                    # This is a simplified approach - in production you'd parse Ogg properly
                    # For now, try to extract payload after Ogg header
                    if len(opus_file) > 100:
                        # Skip Ogg header (typically ~50-100 bytes) and return payload
                        # This is a hack - proper solution would parse Ogg container
                        opus_data = opus_file[100:]  # Skip header
                        return opus_data
                    return opus_file
                finally:
                    os.unlink(temp_opus)
            except Exception as e:
                log.error(f"Pydub encode error: {e}")
                return b""
        else:
            raise RuntimeError("Encoder not initialized")
            
    def get_codec_header(self, sample_rate=None, frame_duration_ms: int = 60) -> bytes:
        """Get codec header for sending audio.
        
        Zello codec_header structure (4 bytes, Little Endian):
        - Bytes 0-1: Sample Rate (uint16, Little Endian) - 16000 = 0x3E80, 8000 = 0x1F40
        - Byte 2: Frames Per Packet (uint8) - 1
        - Byte 3: Frame Duration in ms (uint8) - 20, 40, or 60
        """
        if not self.encoder and not PYDUB_AVAILABLE:
            self.init_encoder()
        
        header_rate = sample_rate if sample_rate is not None else SAMPLE_RATE
        frames_per_packet = 1
        codec_header = struct.pack('<HBB', header_rate, frames_per_packet, frame_duration_ms)
        return codec_header

# ---------------------------------------------------------------------------
# ElevenLabs STT
# ---------------------------------------------------------------------------

class ElevenLabsSTT:
    """Speech-to-Text via ElevenLabs Scribe API."""
    
    def __init__(self):
        self.api_key = ELEVENLABS_API_KEY
        
    async def transcribe(self, pcm_audio: bytes) -> str:
        """Transcribe PCM audio to text."""
        # Build WAV file in memory
        wav = self._build_wav(pcm_audio)
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    ELEVENLABS_STT_URL,
                    headers={"xi-api-key": self.api_key},
                    files={"file": ("audio.wav", wav, "audio/wav")},
                    data={
                        "model_id": "scribe_v1",
                        "filter_profanity": False,
                        "language": "ru",
                    },
                )
                resp.raise_for_status()
                result = resp.json()
                text = result.get("text", "").strip()
                log.info(f"STT result: {text}")
                return text
        except Exception as e:
            log.error(f"STT error: {e}")
            return ""
            
    def _build_wav(self, pcm_data: bytes) -> bytes:
        """Build WAV header around PCM data."""
        data_size = len(pcm_data)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_size, b"WAVE",
            b"fmt ", 16, 1, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
            b"data", data_size,
        )
        return header + pcm_data

# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------

class ElevenLabsTTS:
    """Text-to-Speech via ElevenLabs."""
    
    def __init__(self):
        self.api_key = ELEVENLABS_API_KEY
        self.voice_id = ELEVENLABS_VOICE_ID
        
    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to PCM audio."""
        url = f"{ELEVENLABS_TTS_URL}/{self.voice_id}"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    headers={
                        "xi-api-key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "text": text,
                        "model_id": "eleven_turbo_v2_5",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                        },
                    },
                    params={"output_format": "pcm_16000"},
                )
                resp.raise_for_status()
                audio = resp.content
                log.info(f"TTS generated {len(audio)} bytes")
                return audio
        except Exception as e:
            log.error(f"TTS error: {e}")
            return b""

# ---------------------------------------------------------------------------
# Claude Gateway Client
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Markdown stripper (для голосового вывода)
# ---------------------------------------------------------------------------

def strip_markdown(text: str) -> str:
    """Удаляет markdown-разметку перед отправкой в TTS."""
    # Заголовки
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Жирный/курсив
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_]+)_{1,3}', r'\1', text)
    # Код
    text = re.sub(r'`{1,3}[^`]*`{1,3}', '', text)
    # Ссылки [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # Маркированные списки → запятые
    text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.MULTILINE)
    # Специальные символы
    text = re.sub(r'[→←↓↑►▪▸]', '', text)
    # Множественные пробелы/переносы → один
    text = re.sub(r'\n{2,}', '. ', text)
    text = re.sub(r'\n', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def assess_clarity(transcript: str) -> tuple:
    """Проверяет насколько чёткий транскрипт STT."""
    if not transcript or not transcript.strip():
        return False, "Пустой транскрипт"
    text = transcript.strip()
    words = text.split()
    if len(words) == 1 and len(text) <= 1:
        return False, f"Одна буква: '{text}'"
    if re.match(r'^[\d\s\.\,\!\?\-]+$', text):
        return False, f"Только цифры/знаки: '{text}'"
    if len(words) >= 3:
        unique = set(w.lower() for w in words)
        if len(unique) == 1:
            return False, f"Повторяющийся звук: '{text}'"
    return True, "OK"


# ---------------------------------------------------------------------------
# OpenClaw Bridge — тонкий мост к главному агенту (порт 18789)
# ---------------------------------------------------------------------------

class OpenClawBridge:
    """Отправляет транскрипт в главную сессию OpenClaw и получает ответ с полным доступом к инструментам."""

    GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
    GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
    # Стабильный user для сохранения сессии между вызовами
    SESSION_USER = "zello-valeryklintsou"

    async def chat(self, user_message: str) -> str:
        """Отправить голосовое сообщение в главный агент, получить текстовый ответ."""
        url = f"{self.GATEWAY_URL}/v1/chat/completions"
        # Префикс чтобы агент знал что это голосовой канал
        content = f"[ZELLO ГОЛОС, отвечай кратко без markdown]: {user_message}"
        payload = {
            "model": "openclaw:main",
            "messages": [{"role": "user", "content": content}],
            "user": self.SESSION_USER,
        }
        headers = {
            "Content-Type": "application/json",
        }
        if self.GATEWAY_TOKEN:
            headers["Authorization"] = f"Bearer {self.GATEWAY_TOKEN}"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                log.info(f"OpenClaw response ({len(text)} chars): {text[:120]}")
                return strip_markdown(text)
        except Exception as e:
            log.error(f"OpenClaw bridge error: {e}")
            return "Произошла ошибка при обработке запроса."




# ---------------------------------------------------------------------------
# Zello Client
# ---------------------------------------------------------------------------

class ZelloClient:
    """WebSocket client for Zello Work."""
    
    def __init__(self):
        self.ws = None
        self.opus_codec = OpusCodec()
        self.stt = ElevenLabsSTT()
        self.tts = ElevenLabsTTS()
        self.bridge = OpenClawBridge()
        self.current_stream = None
        self.ignored_stream = None  # Stream ID to ignore (self-sent audio to prevent feedback loops)
        self.pcm_buffer = bytearray()  # Store decoded PCM audio
        self.opus_buffer = bytearray()  # Store raw Opus packets for pydub
        self.outgoing_stream_id = None  # Stream ID for outgoing audio stream
        self.pending_start_seq = None  # Sequence number of pending start_stream
        self.stream_start_event = None  # Event to wait for stream_id (will be created in async context)
        self.pending_audio = None  # Store list of Opus packets to send when stream_id arrives (for late responses)
        self.pending_packet_duration_ms = 60  # Frame duration for pending audio (used when sending late)
        
    async def connect(self):
        """Connect to Zello WebSocket and authenticate."""
        # Create event in async context
        self.stream_start_event = asyncio.Event()
        
        log.info(f"Connecting to {ZELLO_WS_URL}...")
        try:
            self.ws = await websockets.connect(ZELLO_WS_URL)
            log.info("WebSocket connected")
        except Exception as e:
            log.error(f"Failed to connect: {e}")
            raise
        
        # Authenticate — consumer Zello uses auth_token (JWT), not password
        # Consumer endpoint: wss://zello.io/ws
        # Logon includes channel directly (no separate join_channel needed for consumer)
        # Consumer Zello: channels (массив!), auth_token + username + password
        auth_msg = {
            "command": "logon",
            "seq": 1,
            "auth_token": ZELLO_AUTH_TOKEN,
            "username": ZELLO_USERNAME,
            "password": ZELLO_PASSWORD,
            "channels": [ZELLO_CHANNEL],
        }
        log.info(f"Logon: username={ZELLO_USERNAME}, channels=[{ZELLO_CHANNEL}]")
        await self.ws.send(json.dumps(auth_msg))

        # Wait for logon response
        # Success format: {"success": true, "seq": 1, "refresh_token": "..."}
        # Error format:   {"error": "...", "seq": 1}
        try:
            response = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            log.info(f"Auth response: {response}")
            try:
                resp_data = json.loads(response)
                if resp_data.get("success") is True:
                    log.info(f"✅ Logon successful, channel={ZELLO_CHANNEL}")
                    # Save refresh_token for future reconnects
                    refresh_token = resp_data.get("refresh_token", "")
                    if refresh_token:
                        log.info(f"Got refresh_token for fast reconnect")
                elif "error" in resp_data:
                    log.error(f"Logon error: {resp_data['error']}")
                    raise ConnectionError(f"Logon failed: {resp_data}")
                else:
                    log.info(f"Logon response: {resp_data}")
            except json.JSONDecodeError:
                log.warning(f"Non-JSON auth response: {response}")
        except asyncio.TimeoutError:
            log.warning("⚠️ No auth response within 10s — continuing anyway")
        
    async def run(self):
        """Main event loop."""
        await self.connect()
        
        try:
            async for message in self.ws:
                # websockets can receive messages as bytes or str
                # Binary audio packets come as bytes
                if isinstance(message, bytes):
                    # Check if it's valid UTF-8 text (JSON) or binary audio
                    try:
                        # Try to decode as text
                        text = message.decode('utf-8')
                        # If successful, try to parse as JSON
                        try:
                            json.loads(text)
                            # It's valid JSON text, handle as text message
                            await self.handle_message(text)
                        except json.JSONDecodeError:
                            # Not JSON, treat as binary audio
                            log.info(f"🎤 Bytes message is not valid JSON, treating as binary audio: {len(message)} bytes")
                            await self.handle_binary_message(message)
                    except UnicodeDecodeError:
                        # Can't decode as UTF-8, it's binary audio
                        log.info(f"🎤 Bytes message can't be decoded as UTF-8, treating as binary audio: {len(message)} bytes")
                        await self.handle_binary_message(message)
                else:
                    # Text message - JSON command
                    await self.handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            log.info("WebSocket connection closed")
        except Exception as e:
            log.error(f"Error in main loop: {e}")
            
    async def handle_binary_message(self, message: bytes):
        """Handle binary audio packet from Zello.
        
        Zello packet structure (9-byte header):
        - Byte 0: Packet Type (0x01 for audio)
        - Bytes 1-4: Stream ID (uint32, Big Endian)
        - Bytes 5-8: Packet ID (uint32, Big Endian)
        - Bytes 9+: Opus payload
        """
        if self.current_stream is None:
            log.debug(f"Ignoring binary message - no active stream")
            return
        
        # Validate packet structure
        if len(message) < 9:
            log.warning(f"Packet too short: {len(message)} bytes")
            return
        
        # Check packet type (byte 0 should be 0x01 for audio)
        packet_type = message[0]
        if packet_type != 0x01:
            log.debug(f"Unknown packet type: 0x{packet_type:02x}")
            return
        
        # Extract Stream ID (bytes 1-4, Big Endian)
        stream_id = struct.unpack('>I', message[1:5])[0]
        
        # Verify this packet belongs to current stream
        if stream_id != self.current_stream:
            log.debug(f"Packet for different stream: {stream_id} (current: {self.current_stream})")
            return
        
        # Extract Packet ID (bytes 5-8, Big Endian) - for packet loss detection
        packet_id = struct.unpack('>I', message[5:9])[0]
        
        # Debug: log first packet structure
        if packet_id == 0:
            opus_payload = message[9:] if len(message) > 9 else b""
            toc_byte = opus_payload[0] if len(opus_payload) > 0 else 0
            log.info(f"🔍 First INCOMING packet structure: type=0x{packet_type:02x}, stream_id={stream_id}, packet_id={packet_id}, total_len={len(message)}")
            log.info(f"🔍 First 20 bytes (hex): {message[:20].hex()}")
            log.info(f"🔍 Bytes 9-20 (hex): {message[9:20].hex() if len(message) > 20 else message[9:].hex()}")
            log.info(f"🔍 First INCOMING Opus TOC byte: 0x{toc_byte:02x} (binary: {toc_byte:08b})")
        
        # Extract Opus payload (skip 9-byte header)
        # According to technical report, structure is FIXED:
        # Byte 0: Packet Type (0x01)
        # Bytes 1-4: Stream ID (Big Endian)
        # Bytes 5-8: Packet ID (Big Endian)
        # Bytes 9+: Opus payload (NO additional headers!)
        # 
        # CRITICAL: Header is exactly 9 bytes. No additional offsets needed.
        # Searching for TOC at random offsets is incorrect and will cause corruption.
        opus_data = message[9:]
        
        if len(opus_data) == 0:
            log.warning(f"Empty Opus payload in packet {packet_id}")
            return
        
        # Debug: log first few bytes to verify structure
        if packet_id == 0 or packet_id % 50 == 0:
            toc = opus_data[0] if len(opus_data) > 0 else 0
            # Parse TOC according to RFC 6716
            # Bits 7-3: Config ID (5 bits)
            # Zello TOC byte format (may differ from RFC 6716):
            # Bits 0-2: Config (3 bits) - but shifted
            # Bit 3: Stereo flag
            # Bits 4-5: Frame count code (or other flags)
            # Note: Old parsing showed Config=0, Stereo=1 for 0x18
            # Let's use the format that worked: Config in bits 3-7, Stereo in bit 2
            config_id = (toc >> 3) & 0x1F  # Bits 3-7 (5 bits)
            stereo = (toc >> 2) & 0x01  # Bit 2
            frame_count_code = toc & 0x03  # Bits 0-1
            log.debug(f"Packet {packet_id} Opus data: first 10 bytes = {opus_data[:10].hex()}, TOC=0x{toc:02x}, Config={config_id}, Stereo={stereo}, Frames={frame_count_code}")
        
        # Check if first byte looks like valid Opus TOC
        # Valid TOC: config (bits 0-2) should be 0-7, frame count (bits 4-6) should be 0-3
        # Also check that TOC byte is reasonable (not too high values)
        toc = opus_data[0]
        toc_config = toc & 0x07
        toc_frame_count = (toc >> 4) & 0x07
        toc_stereo = (toc >> 3) & 0x01
        
        # Note: TOC byte 0x18 is technically valid (config=0, frames=1, stereo=1)
        # But according to technical report, after 9-byte header should be valid Opus packets
        # The data starts with 0x18, which might be a valid TOC, but decoding fails
        # This suggests that either:
        # 1. There are additional bytes after 9-byte header before Opus payload
        # 2. The Opus packets need to be decoded differently
        # 3. The packets are concatenated and need to be split
        
        # According to technical report, Zello sends raw Opus packets with TOC byte
        # Let's try to decode each packet individually if opuslib is available
        # For now, store original data
        original_opus_data = opus_data
        
        # Try to find valid Opus TOC by checking if 0x18 is actually valid
        # 0x18 = 00011000 binary
        # Bits 0-2 (config): 000 = 0 (valid, 0-7)
        # Bit 3 (stereo): 1 = stereo
        # Bits 4-6 (frame count): 001 = 1 (valid, 0-3)
        # So 0x18 is a valid TOC byte for mono/stereo Opus packet
        # But maybe there's an issue with how we're trying to decode it
        
        # Try to decode immediately if opuslib is available
        if OPUS_AVAILABLE and self.opus_codec.decoder:
            # Try decoding with current offset first
            try:
                pcm_chunk = self.opus_codec.decode(opus_data)
                if pcm_chunk and len(pcm_chunk) > 0:
                    self.pcm_buffer.extend(pcm_chunk)
                    log.info(f"✅ Decoded packet {packet_id}: {len(opus_data)} bytes Opus -> {len(pcm_chunk)} bytes PCM")
                    return  # Successfully decoded, don't add to opus_buffer
            except Exception as e:
                log.debug(f"opuslib decode failed for packet {packet_id} at offset 0: {e}")
            
            # If failed, try with additional offsets (maybe there's extra header)
            if len(opus_data) > 10:
                for skip in [2, 4, 6, 8]:
                    if len(opus_data) > skip:
                        try:
                            test_data = opus_data[skip:]
                            pcm_chunk = self.opus_codec.decode(test_data)
                            if pcm_chunk and len(pcm_chunk) > 0:
                                self.pcm_buffer.extend(pcm_chunk)
                                log.info(f"✅ Decoded packet {packet_id} with offset {skip}: {len(test_data)} bytes Opus -> {len(pcm_chunk)} bytes PCM")
                                return
                        except Exception as e:
                            log.debug(f"opuslib decode failed for packet {packet_id} at offset {skip}: {e}")
                            continue
        
        # If immediate decode failed or opuslib not available, add to buffer for batch processing
        self.opus_buffer.extend(opus_data)
        # Log first packet and every 20th packet
        if len(self.opus_buffer) == len(opus_data) or len(self.opus_buffer) % (len(opus_data) * 20) < len(opus_data):
            log.info(f"🎤 Binary audio packet {packet_id}: {len(message)} bytes -> {len(opus_data)} bytes Opus, total buffer: {len(self.opus_buffer)} bytes")
    
    def _extract_opus_from_packet(self, packet: bytes) -> bytes:
        """Extract Opus audio data from Zello packet structure."""
        if len(packet) < 4:
            return packet  # Too short, return as-is
        
        # Pattern observed in logs: 
        # - 1f0000000018c115... or 1c0000000018c127... (18 c1)
        # - 0018e11eae966e4f... (18 e1)
        # Looks like: [header 4-8 bytes] [18 XX] [opus_data...]
        # Try to find the pattern "18 XX" which might mark start of Opus data
        
        # Pattern 1: Look for "18 XX" marker where XX can be various values
        for i in range(min(20, len(packet) - 1)):
            if packet[i] == 0x18:
                # Found 0x18, check next byte and skip both
                opus_candidate = packet[i+2:]
                if len(opus_candidate) > 0:
                    # Validate TOC byte
                    toc = opus_candidate[0]
                    if (toc & 0x07) <= 7 and ((toc >> 4) & 0x07) <= 3:
                        return opus_candidate
        
        # Pattern 2: Look for "18 XX YY" where YY might be part of marker
        for i in range(min(20, len(packet) - 2)):
            if packet[i] == 0x18:
                # Found 0x18, try skipping 2 or 3 bytes
                for skip in [2, 3]:
                    if i + skip < len(packet):
                        opus_candidate = packet[i+skip:]
                        if len(opus_candidate) > 0:
                            toc = opus_candidate[0]
                            if (toc & 0x07) <= 7 and ((toc >> 4) & 0x07) <= 3:
                                return opus_candidate
        
        # Pattern 3: First 2 bytes as little-endian length/header
        try:
            header_val = struct.unpack('<H', packet[0:2])[0]  # First 2 bytes as uint16
            # If it's a reasonable length (20-200 bytes for Opus packet), skip it
            if 20 <= header_val <= 200 and header_val < len(packet):
                opus_candidate = packet[2:]
                if len(opus_candidate) > 0:
                    # Check if it looks like Opus (TOC byte validation)
                    toc = opus_candidate[0]
                    if (toc & 0x07) <= 7 and ((toc >> 4) & 0x07) <= 3:
                        return opus_candidate
        except:
            pass
        
        # Pattern 4: 01 00 00 XX (length?) ... (opus_data)
        if packet[0] == 0x01 and packet[1] == 0x00 and packet[2] == 0x00:
            # Skip header (try 4, 8, 12 bytes)
            for header_size in [4, 8, 12, 16]:
                if len(packet) > header_size:
                    opus_candidate = packet[header_size:]
                    # Check if it looks like Opus (TOC byte validation)
                    if len(opus_candidate) > 0:
                        toc = opus_candidate[0]
                        # Valid Opus TOC: config 0-7, frame count 0-3
                        if (toc & 0x07) <= 7 and ((toc >> 4) & 0x07) <= 3:
                            return opus_candidate
        
        # Pattern 5: Try reading length from bytes 2-4 (after 00 18)
        try:
            if len(packet) > 4 and packet[0] == 0x00:
                # Try length at offset 2
                length = struct.unpack('<H', packet[2:4])[0]  # 2-byte length at offset 2
                if 4 <= length < len(packet) and length < 10000:
                    # Skip header (2 bytes) + length field (2 bytes) + maybe more
                    for skip in [4, 6, 8]:
                        if skip < len(packet):
                            opus_candidate = packet[skip:]
                            if len(opus_candidate) > 0:
                                toc = opus_candidate[0]
                                if (toc & 0x07) <= 7 and ((toc >> 4) & 0x07) <= 3:
                                    return opus_candidate
        except:
            pass
        
        # Pattern 4: Try skipping first 2, 4, 6, 8 bytes and check for Opus TOC
        for skip in [2, 4, 6, 8]:
            if len(packet) > skip:
                opus_candidate = packet[skip:]
                if len(opus_candidate) > 0:
                    toc = opus_candidate[0]
                    # Valid Opus TOC: config 0-7, frame count 0-3
                    if (toc & 0x07) <= 7 and ((toc >> 4) & 0x07) <= 3:
                        return opus_candidate
        
        # Last resort: return as-is (might be already Opus)
        return packet
    
    def _create_ogg_container(self, opus_data: bytes, codec_header: Optional[bytes] = None) -> Optional[str]:
        """Create Ogg Opus container from raw Opus packets.
        
        According to technical report, we need to create a minimal Ogg container
        with proper Ogg page headers, checksums, and OpusHead/OpusTags packets.
        This is complex but necessary for ffmpeg to decode raw Opus packets.
        """
        try:
            import tempfile
            import zlib
            
            # Create Ogg file
            ogg_file = tempfile.NamedTemporaryFile(suffix='.ogg', delete=False)
            ogg_file.close()
            ogg_path = ogg_file.name
            
            # Parse codec_header to get sample rate
            sample_rate = SAMPLE_RATE
            if codec_header and len(codec_header) >= 4:
                sample_rate = struct.unpack('<H', codec_header[0:2])[0]
                if sample_rate == 16001:
                    sample_rate = 16000
            
            # Try to create minimal Ogg Opus container manually
            # This is a simplified version - proper implementation would be more complex
            try:
                with open(ogg_path, 'wb') as f:
                    # Ogg Page Header structure (simplified)
                    # Capture Pattern: "OggS" (4 bytes)
                    f.write(b'OggS')
                    # Version: 0 (1 byte)
                    f.write(b'\x00')
                    # Header Type: 0x02 = BOS (Beginning of Stream) for first page
                    f.write(b'\x02')
                    # Granule Position: 0 (8 bytes, little endian)
                    f.write(struct.pack('<Q', 0))
                    # Serial Number: arbitrary (4 bytes, little endian)
                    f.write(struct.pack('<I', 0x12345678))
                    # Page Sequence Number: 0 (4 bytes, little endian)
                    f.write(struct.pack('<I', 0))
                    # Checksum: will calculate later (4 bytes, initially 0)
                    checksum_pos = f.tell()
                    f.write(b'\x00\x00\x00\x00')
                    # Page Segments: 1 segment (1 byte)
                    f.write(b'\x01')
                    # Segment Table: size of OpusHead (1 byte)
                    opushead_size = 19  # Standard OpusHead size
                    f.write(bytes([opushead_size]))
                    
                    # OpusHead packet
                    # Magic Signature: "OpusHead" (8 bytes)
                    f.write(b'OpusHead')
                    # Version: 1 (1 byte)
                    f.write(b'\x01')
                    # Output Channel Count: 1 (mono) (1 byte)
                    f.write(bytes([CHANNELS]))
                    # Pre-skip: 0 (2 bytes, little endian)
                    f.write(struct.pack('<H', 0))
                    # Input Sample Rate: 16000 (4 bytes, little endian)
                    f.write(struct.pack('<I', sample_rate))
                    # Output Gain: 0 (2 bytes, little endian)
                    f.write(struct.pack('<h', 0))
                    # Channel Mapping: 0 = RTP mapping (1 byte)
                    f.write(b'\x00')
                    
                    # Calculate and write checksum for first page
                    f.seek(0)
                    page_data = f.read()
                    f.seek(0, 2)  # Seek to end
                    
                    # Calculate CRC32 checksum (Ogg uses modified CRC32)
                    # Ogg uses a specific CRC32 polynomial: 0x04c11db7
                    # For now, use standard CRC32 - might need adjustment
                    page_without_crc = page_data[:checksum_pos] + b'\x00\x00\x00\x00' + page_data[checksum_pos+4:]
                    crc = zlib.crc32(page_without_crc) & 0xffffffff
                    f.seek(checksum_pos)
                    f.write(struct.pack('<I', crc))
                    f.seek(0, 2)  # Seek to end
                    
                    # Second page: OpusTags (optional but recommended)
                    # For now, skip OpusTags and go straight to audio data
                    
                    # Third page: Audio data
                    # Split opus_data into pages (max 255 segments per page, max 255 bytes per segment)
                    # For simplicity, put all data in one page
                    page_start = f.tell()
                    f.write(b'OggS')
                    f.write(b'\x00')  # Version
                    f.write(b'\x00')  # Header Type: 0 = continuation
                    # Granule Position: calculate based on frame duration
                    # 60ms at 16kHz = 960 samples
                    num_frames = len(opus_data) // 100  # Rough estimate
                    granule = num_frames * 960
                    f.write(struct.pack('<Q', granule))
                    f.write(struct.pack('<I', 0x12345678))  # Serial
                    f.write(struct.pack('<I', 1))  # Page sequence
                    checksum_pos2 = f.tell()
                    f.write(b'\x00\x00\x00\x00')  # Checksum placeholder
                    
                    # Calculate segments needed
                    segments = []
                    remaining = len(opus_data)
                    while remaining > 0:
                        seg_size = min(255, remaining)
                        segments.append(seg_size)
                        remaining -= seg_size
                    
                    f.write(bytes([len(segments)]))  # Page segments count
                    for seg_size in segments:
                        f.write(bytes([seg_size]))  # Segment sizes
                    
                    # Write Opus data
                    f.write(opus_data)
                    
                    # Calculate checksum for second page
                    f.seek(page_start)
                    page2_data = f.read()
                    f.seek(0, 2)
                    crc2 = zlib.crc32(page2_data[:checksum_pos2] + page2_data[checksum_pos2+4:]) & 0xffffffff
                    f.seek(checksum_pos2)
                    f.write(struct.pack('<I', crc2))
                
                # Verify file was created
                if os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
                    log.info(f"Created minimal Ogg container: {len(opus_data)} bytes Opus -> {os.path.getsize(ogg_path)} bytes Ogg")
                    return ogg_path
                else:
                    if os.path.exists(ogg_path):
                        os.unlink(ogg_path)
                    return None
                    
            except Exception as e:
                log.debug(f"Manual Ogg creation failed: {e}")
                if os.path.exists(ogg_path):
                    os.unlink(ogg_path)
            
            # Fallback: try opusenc if available
            raw_file = tempfile.NamedTemporaryFile(suffix='.raw', delete=False)
            raw_file.write(opus_data)
            raw_file.close()
            raw_path = raw_file.name
            
            try:
                result = subprocess.run(
                    ["opusenc", "--raw", "--raw-rate", str(sample_rate), "--raw-chan", str(CHANNELS),
                     "--raw-bits", "16", raw_path, ogg_path],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0 and os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
                    log.info(f"Created Ogg container via opusenc: {len(opus_data)} bytes Opus -> {os.path.getsize(ogg_path)} bytes Ogg")
                    os.unlink(raw_path)
                    return ogg_path
            except FileNotFoundError:
                pass
            finally:
                if os.path.exists(raw_path):
                    os.unlink(raw_path)
            
            # If all failed, clean up and return None
            if os.path.exists(ogg_path):
                os.unlink(ogg_path)
            log.warning("Failed to create Ogg container with all methods")
            return None
            
        except Exception as e:
            log.error(f"Failed to create Ogg container: {e}", exc_info=True)
            return None
    
    async def handle_message(self, message):
        """Handle incoming WebSocket message."""
        try:
            # Log message type for debugging
            msg_type = type(message).__name__
            if isinstance(message, bytes):
                log.debug(f"handle_message received bytes: {len(message)} bytes, first 10: {message[:10]}")
            elif isinstance(message, str):
                log.debug(f"handle_message received str: {len(message)} chars, first 50: {message[:50]}")
            else:
                log.warning(f"handle_message received unknown type: {msg_type}")
            
            # Check if message is bytes (binary) - websockets might send bytes
            if isinstance(message, bytes):
                # Try to decode as text first, if fails - it's binary audio
                try:
                    text = message.decode('utf-8')
                    # Try to parse as JSON
                    try:
                        data = json.loads(text)
                        # It's valid JSON, continue processing
                        message = text
                    except json.JSONDecodeError:
                        # Not JSON, treat as binary audio
                        log.info(f"🎤 Bytes message is not valid JSON, treating as binary: {len(message)} bytes")
                        await self.handle_binary_message(message)
                        return
                except (UnicodeDecodeError, UnicodeError):
                    # Can't decode as UTF-8, it's binary audio
                    log.info(f"🎤 Bytes message can't be decoded as UTF-8, treating as binary: {len(message)} bytes")
                    await self.handle_binary_message(message)
                    return
            
            # Now it's definitely a string, parse as JSON
            try:
                data = json.loads(message)
            except (json.JSONDecodeError, TypeError) as e:
                # If it's not JSON, it might be binary audio data
                # Check if it looks like binary (starts with non-printable chars)
                if isinstance(message, str) and len(message) > 0:
                    first_char = ord(message[0])
                    if first_char < 32 or first_char > 126:
                        # Looks like binary, convert to bytes and handle
                        log.info(f"🎤 String message looks like binary, treating as binary: {len(message)} chars")
                        await self.handle_binary_message(message.encode('latin-1'))
                        return
                # Real JSON error, log it but don't raise - might be binary
                log.warning(f"JSON decode error: {e}, message type: {type(message)}, first 50: {str(message)[:50]}")
                # Try to handle as binary anyway
                if isinstance(message, str):
                    await self.handle_binary_message(message.encode('latin-1'))
                return
            command = data.get("command")
            
            # Handle start_stream response FIRST (it comes without "command" field)
            # Response format: {"stream_id": X, "success": true, "seq": Y}
            # Check if this is a response to our start_stream (even if it comes late)
            received_seq = data.get("seq")
            
            # Check if this looks like a start_stream response (has stream_id, success, seq, but no command)
            if "stream_id" in data and "success" in data and received_seq is not None:
                log.info(f"🔍 Potential start_stream response: seq={received_seq}, pending_seq={self.pending_start_seq}, keys={list(data.keys())}")
                
                # Check if this matches our pending request (even if it came late)
                if self.pending_start_seq is not None and received_seq == self.pending_start_seq:
                    self.outgoing_stream_id = data.get("stream_id")
                    log.info(f"✅ Stream started successfully (stream_id={self.outgoing_stream_id}) - LATE RESPONSE HANDLED!")
                    self.pending_start_seq = None
                    if self.stream_start_event:
                        self.stream_start_event.set()  # Signal that stream_id is ready
                    
                    # If we have pending audio, send it now
                    if self.pending_audio is not None:
                        total_bytes = sum(len(p) for p in self.pending_audio) if isinstance(self.pending_audio, list) else 0
                        log.info(f"📤 Sending pending audio ({len(self.pending_audio)} packets, {total_bytes} bytes) now that stream_id is available")
                        # Create a task to send audio (don't await to avoid blocking)
                        asyncio.create_task(self._send_pending_audio(self.pending_audio, self.outgoing_stream_id, self.pending_packet_duration_ms))
                        self.pending_audio = None
                    
                    return  # Don't process further
                elif "error" in data:
                    log.error(f"❌ Stream start failed: {data.get('error')}")
                    self.pending_start_seq = None
                    self.outgoing_stream_id = None
                    if self.stream_start_event:
                        self.stream_start_event.set()  # Signal even on error to unblock
                    return  # Don't process further
                else:
                    # This looks like a start_stream response but seq doesn't match
                    log.warning(f"⚠️ Received start_stream-like response but seq mismatch: received={received_seq}, pending={self.pending_start_seq}")
            
            # Log ALL commands for debugging (we need to see everything)
            if command == "on_audio":
                # Log first audio packet and periodically
                if len(self.opus_buffer) == 0:
                    stream_id = data.get("stream_id")
                    log.info(f"🎤 FIRST audio packet! stream_id={stream_id}, current={self.current_stream}")
            else:
                # Log all non-audio commands with full details
                log.info(f"📨 Command: {command}")
                # Show all keys to understand structure
                log.info(f"   Keys: {list(data.keys())}")
                # Show full data for important events
                if command in ["on_stream_start", "on_stream_stop", "on_text_message", "on_channel_status"]:
                    log.info(f"   Full data:\n{json.dumps(data, indent=2)}")
            
            if command == "on_channel_status":
                log.info(f"Channel status: {data}")
                users = data.get("users", [])
                log.info(f"Users in channel: {len(users)}")
                
            elif command == "on_logon_result":
                status = data.get("status")
                if status == "ok":
                    log.info("Logged in successfully")
                else:
                    log.error(f"Login failed: {data}")
                    
            elif command == "on_error":
                log.error(f"Zello error: {data}")
                
            elif command == "on_stream_start":
                stream_id = data.get("stream_id")
                from_user = data.get("from", "unknown")
                log.info(f"Stream started: stream_id={stream_id}, from={from_user}")
                # Ignore audio from self to prevent feedback loops
                if from_user.lower() == ZELLO_USERNAME.lower():
                    log.info(f"Ignoring audio from self ({from_user}) — skipping to prevent loop")
                    self.ignored_stream = stream_id
                    self.current_stream = None
                else:
                    self.ignored_stream = None
                    self.current_stream = stream_id
                codec_header = data.get("codec_header")
                if codec_header:
                    # Decode base64 codec header
                    codec_bytes = base64.b64decode(codec_header)
                    log.info(f"Codec header received: {len(codec_bytes)} bytes")
                    try:
                        self.opus_codec.init_decoder(codec_bytes)
                        log.info("✅ Decoder initialized successfully")
                    except Exception as e:
                        log.error(f"Failed to initialize decoder: {e}", exc_info=True)
                        # Don't raise - continue without decoder, will use ffmpeg later
                else:
                    log.warning("No codec_header in stream_start")
                self.pcm_buffer.clear()
                self.opus_buffer.clear()
                
            elif command == "on_audio":
                stream_id = data.get("stream_id")
                if stream_id == self.current_stream:
                    audio_data = data.get("audio")
                    if audio_data:
                        # Decode base64 audio
                        opus_packet = base64.b64decode(audio_data)
                        self.opus_buffer.extend(opus_packet)
                        # Log every 20th packet to avoid spam but still see progress
                        if len(self.opus_buffer) % (len(opus_packet) * 20) < len(opus_packet):
                            log.info(f"🎤 Audio packet: {len(opus_packet)} bytes, total buffer: {len(self.opus_buffer)} bytes")
                else:
                    log.warning(f"Ignoring audio from stream {stream_id} (current: {self.current_stream})")
                        
            elif command == "on_stream_stop":
                stream_id = data.get("stream_id")
                log.info(f"Stream stopped: stream_id={stream_id}, opus_buffer={len(self.opus_buffer)} bytes, pcm_buffer={len(self.pcm_buffer)} bytes")
                if self.current_stream == stream_id and (len(self.pcm_buffer) > 0 or len(self.opus_buffer) > 0):
                    # Capture current buffers and process in background to avoid blocking the main loop
                    p_pcm = bytes(self.pcm_buffer) if self.pcm_buffer else None
                    p_opus = bytes(self.opus_buffer) if self.opus_buffer else None
                    asyncio.create_task(self.process_audio_stream(pcm_data=p_pcm, opus_data=p_opus))
                else:
                    log.warning(f"Stream stop for different stream_id or empty buffers")
                self.current_stream = None
                self.pcm_buffer.clear()
                self.opus_buffer.clear()
                
            elif command == "on_text_message":
                text = data.get("text", "")
                from_user = data.get("from", "")
                log.info(f"Text message from {from_user}: {text}")
                await self.handle_text_message(text, from_user)
                
            elif "error" in data:
                # Log errors with full details
                error_msg = data.get("error", "")
                error_seq = data.get("seq", "unknown")
                log.error(f"❌ Zello ERROR (seq={error_seq}): {error_msg}")
                log.error(f"   Full error data: {json.dumps(data, indent=2)}")
                
            else:
                # Log unknown commands
                log.info(f"❓ Unknown command: {command}, data keys: {list(data.keys())}")
                if "error" in data:
                    log.error(f"   Error in unknown command: {data.get('error', '')}")
                
        except json.JSONDecodeError:
            log.warning(f"Invalid JSON: {message}")
        except Exception as e:
            log.error(f"Error handling message: {e}")
            
    async def process_audio_stream(self, pcm_data=None, opus_data=None):
        """Process complete audio stream: decode → STT → LLM → TTS → encode → send."""
        # If we have PCM already (opuslib), use it
        # Otherwise decode Opus buffer (pydub)
        if pcm_data:
            log.info(f"Processing audio stream ({len(pcm_data)} bytes PCM from task)")
            pcm_audio = pcm_data
        elif len(self.pcm_buffer) > 0:
            log.info(f"Processing audio stream ({len(self.pcm_buffer)} bytes PCM)")
            pcm_audio = bytes(self.pcm_buffer)
        elif opus_data or len(self.opus_buffer) > 0:
            raw_opus = opus_data if opus_data else bytes(self.opus_buffer)
            log.info(f"Processing audio stream ({len(raw_opus)} bytes Opus)")
            # Decode entire Opus stream with ffmpeg
            try:
                if FFMPEG_AVAILABLE:
                    # Save Opus to temp file
                    opus_data = raw_opus
                    
                    # Note: opuslib is not available (requires Opus library installation)
                    # We'll use ffmpeg for decoding
                    log.debug("opuslib not available, using ffmpeg for decoding...")
                    pcm_audio = None
                    
                    # If opuslib failed or not available, use ffmpeg
                    if not pcm_audio:
                        with tempfile.NamedTemporaryFile(suffix='.opus', delete=False) as f:
                            f.write(opus_data)
                            temp_opus = f.name
                    
                    # Decode with ffmpeg to PCM
                    with tempfile.NamedTemporaryFile(suffix='.pcm', delete=False) as f:
                        temp_pcm = f.name
                    
                    try:
                        # Zello sends Opus packets in a custom format (not standard Ogg)
                        # The data starts with custom headers (e.g., 0100000b7f...)
                        # We need to extract actual Opus packets and create Ogg container
                        # OR use opusdec/opus-tools if available
                        # For now, try to strip custom headers and create minimal Ogg
                        
                        # Read raw data
                        with open(temp_opus, "rb") as f:
                            raw_data = f.read()
                        
                        log.info(f"Raw Opus data: {len(raw_data)} bytes, first 20 bytes: {raw_data[:20].hex()}")
                        
                        # Use codec_header if available to understand format
                        codec_header = self.opus_codec.codec_header
                        if codec_header:
                            log.info(f"Using codec_header: {len(codec_header)} bytes, hex: {codec_header[:20].hex() if len(codec_header) >= 20 else codec_header.hex()}")
                        
                        # Try to find Opus packets - they usually start with specific patterns
                        # Zello format might have packet headers - try skipping first few bytes
                        # Common Opus packet starts: look for TOC (Table of Contents) byte patterns
                        opus_packets = []
                        offset = 0
                        
                        # Try to decode raw Opus packets directly with ffmpeg
                        # ffmpeg can decode raw Opus if we specify the format correctly
                        # According to technical report, data after 9-byte header should be valid Opus
                        # But we're seeing data starting with 18e1... which might not be valid Opus TOC
                        # Try different approaches:
                        
                        # Approach 1: Try to decode each Opus packet individually
                        # According to technical report, each packet after 9-byte header is a valid Opus packet
                        # But we're concatenating all packets, which might not work
                        # Try to split packets and decode each one, then concatenate PCM
                        pcm_audio = None
                        if FFMPEG_AVAILABLE:
                            # Try to decode by treating the entire buffer as a stream of Opus packets
                            # First, try creating an Ogg container with all packets
                            # Use opusenc-like approach: create Ogg pages for each packet
                            
                            # Method 1: Try using ffmpeg to create Ogg from raw Opus packets
                            # ffmpeg can create Ogg Opus from raw packets if we use the right format
                            try:
                                # Save raw data
                                raw_file = tempfile.NamedTemporaryFile(suffix='.raw', delete=False)
                                raw_file.write(raw_data)
                                raw_file.close()
                                raw_path = raw_file.name
                                
                                # Create Ogg file
                                ogg_file = tempfile.NamedTemporaryFile(suffix='.ogg', delete=False)
                                ogg_file.close()
                                ogg_path = ogg_file.name
                                
                                # Try ffmpeg to create Ogg container from raw Opus
                                # Use opus muxer to create Ogg container
                                result = subprocess.run(
                                    [
                                        "ffmpeg",
                                        "-f", "s16le",  # Input as raw PCM first (won't work, but let's try)
                                        "-ar", str(self.opus_codec.sample_rate),
                                        "-ac", str(CHANNELS),
                                        "-i", raw_path,
                                        "-c:a", "libopus",
                                        "-b:a", "16k",
                                        "-frame_duration", "60",
                                        "-application", "voip",
                                        "-y",
                                        ogg_path
                                    ],
                                    capture_output=True,
                                    timeout=5
                                )
                                
                                # That won't work - we need to decode, not encode
                                # Instead, try to use ffmpeg's ability to read raw Opus if we create proper Ogg
                                
                                # Method 2: Try using opusenc if available (from opus-tools)
                                try:
                                    result = subprocess.run(
                                        [
                                            "opusenc",
                                            "--raw",
                                            "--raw-rate", str(self.opus_codec.sample_rate),
                                            "--raw-chan", str(CHANNELS),
                                            "--raw-bits", "16",
                                            raw_path,
                                            ogg_path
                                        ],
                                        capture_output=True,
                                        text=True,
                                        timeout=5
                                    )
                                    if result.returncode == 0 and os.path.exists(ogg_path):
                                        # Now decode the Ogg file
                                        result2 = subprocess.run(
                                            [
                                                "ffmpeg",
                                                "-i", ogg_path,
                                                "-f", "s16le",
                                                "-ar", str(self.opus_codec.sample_rate),
                                                "-ac", str(CHANNELS),
                                                "-"
                                            ],
                                            capture_output=True,
                                            timeout=5
                                        )
                                        if result2.returncode == 0 and len(result2.stdout) > 0:
                                            pcm_audio = result2.stdout
                                            log.info(f"✅ Successfully decoded via opusenc+ffmpeg: {len(raw_data)} bytes -> {len(pcm_audio)} bytes PCM")
                                except FileNotFoundError:
                                    pass
                                
                                # Cleanup
                                if os.path.exists(raw_path):
                                    os.unlink(raw_path)
                                if os.path.exists(ogg_path):
                                    if pcm_audio:
                                        os.unlink(ogg_path)
                                    else:
                                        os.unlink(ogg_path)
                            except Exception as e:
                                log.debug(f"opusenc approach failed: {e}")
                            
                            if not pcm_audio:
                                log.warning("ffmpeg direct decode failed, trying other methods...")
                        
                        # Approach 2: Try using pyogg.OpusFile with Ogg container
                        if not pcm_audio and PYOGG_AVAILABLE:
                            log.info("Trying to decode using pyogg.OpusFile...")
                            try:
                                # Create Ogg container first
                                ogg_file = self._create_ogg_container(raw_data, codec_header)
                                if ogg_file and os.path.exists(ogg_file):
                                    try:
                                        opus_file = pyogg.OpusFile(ogg_file)
                                        pcm_audio = opus_file.as_array().tobytes()
                                        log.info(f"✅ Decoded {len(pcm_audio)} bytes PCM using pyogg.OpusFile")
                                    except Exception as e:
                                        log.debug(f"pyogg.OpusFile failed: {e}")
                                    
                                    try:
                                        os.unlink(ogg_file)
                                    except:
                                        pass
                                
                                if not pcm_audio:
                                    log.warning("pyogg.OpusFile failed, trying ffmpeg...")
                            except Exception as e:
                                log.warning(f"pyogg decoding error: {e}, trying ffmpeg...")
                        
                        # If pyogg failed or not available, try ffmpeg with different offsets
                        result = None
                        if not pcm_audio:
                            # Try different offsets to find valid Opus packets
                            # Opus TOC byte: bit 0-2 = config (0-7), bit 3 = stereo flag, bit 4-6 = frame count
                            for skip in [0, 4, 8, 12, 16, 20, 24]:
                                if skip >= len(raw_data):
                                    break
                                test_data = raw_data[skip:]
                                # Check if it looks like Opus (TOC byte should be reasonable)
                                if len(test_data) > 0:
                                    toc = test_data[0]
                                    # Valid TOC: config 0-7, frame count 0-3
                                    if (toc & 0x07) <= 7 and ((toc >> 4) & 0x07) <= 3:
                                        log.info(f"Trying offset {skip}, TOC byte: 0x{toc:02x}")
                                        # Try decoding with this offset
                                        with tempfile.NamedTemporaryFile(suffix='.opus', delete=False) as f:
                                            f.write(test_data)
                                            test_file = f.name
                                        
                                        # Try different input formats
                                        for input_fmt in ["opus", "libopus", "ogg"]:
                                            cmd = [
                                                "ffmpeg",
                                                "-f", input_fmt,
                                                "-ar", str(SAMPLE_RATE),
                                                "-ac", str(CHANNELS),
                                                "-i", test_file,
                                                "-f", "s16le",
                                                "-ar", str(SAMPLE_RATE),
                                                "-ac", str(CHANNELS),
                                                "-y",
                                                temp_pcm
                                            ]
                                            result = subprocess.run(
                                                cmd,
                                                capture_output=True,
                                                text=True,
                                                timeout=5
                                            )
                                            try:
                                                os.unlink(test_file)
                                            except:
                                                pass
                                            
                                            if result.returncode == 0:
                                                log.info(f"✅ Successfully decoded with offset {skip} and format {input_fmt}")
                                                break
                                        
                                        if result and result.returncode == 0:
                                            break
                        
                        # If all offsets failed, try creating Ogg container manually
                        if result.returncode != 0:
                            log.warning("ffmpeg failed with all offsets, trying to create Ogg container manually...")
                            
                            # Create minimal Ogg Opus container from raw Opus packets
                            ogg_file = self._create_ogg_container(raw_data, codec_header)
                            if ogg_file:
                                # Try decoding the Ogg file
                                cmd = [
                                    "ffmpeg",
                                    "-i", ogg_file,
                                    "-f", "s16le",
                                    "-ar", str(SAMPLE_RATE),
                                    "-ac", str(CHANNELS),
                                    "-y",
                                    temp_pcm
                                ]
                                result = subprocess.run(
                                    cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=10
                                )
                                try:
                                    os.unlink(ogg_file)
                                except:
                                    pass
                                
                                if result.returncode == 0:
                                    log.info("✅ Successfully decoded using created Ogg container")
                                else:
                                    log.warning(f"Ogg container decode failed: {result.stderr[:200]}")
                            
                            # If Ogg container creation failed, try direct Opus decoding with different approaches
                            if result.returncode != 0:
                                log.warning("Ogg container creation failed, trying direct Opus packet decoding...")
                                
                                # Try decoding as raw Opus stream using ffmpeg with different parameters
                                # The data might need to be processed differently
                                # Try skipping potential Zello headers (first 2-8 bytes)
                                for skip_bytes in [0, 2, 4, 6, 8]:
                                    if skip_bytes >= len(raw_data):
                                        break
                                    
                                    test_data = raw_data[skip_bytes:]
                                    if len(test_data) < 10:
                                        continue
                                    
                                    # Save test data
                                    test_file = tempfile.NamedTemporaryFile(suffix='.opus', delete=False)
                                    test_file.write(test_data)
                                    test_file.close()
                                    test_path = test_file.name
                                    
                                    # Try decoding with different formats
                                    for fmt in ["opus", "libopus"]:
                                        cmd = [
                                            "ffmpeg",
                                            "-f", fmt,
                                            "-ar", str(SAMPLE_RATE),
                                            "-ac", str(CHANNELS),
                                            "-i", test_path,
                                            "-f", "s16le",
                                            "-ar", str(SAMPLE_RATE),
                                            "-ac", str(CHANNELS),
                                            "-y",
                                            temp_pcm
                                        ]
                                        result = subprocess.run(
                                            cmd,
                                            capture_output=True,
                                            text=True,
                                            timeout=5
                                        )
                                        
                                        if result.returncode == 0:
                                            log.info(f"✅ Successfully decoded with skip={skip_bytes} bytes, format={fmt}")
                                            try:
                                                os.unlink(test_path)
                                            except:
                                                pass
                                            break
                                    
                                    if result.returncode == 0:
                                        break
                                    
                                    try:
                                        os.unlink(test_path)
                                    except:
                                        pass
                                
                                # If still failed, try opusdec as last resort
                                if result.returncode != 0:
                                    log.warning("Trying opusdec as last resort...")
                                    try:
                                        # opusdec can decode raw Opus files
                                        # Save raw data to file
                                        raw_opus_file = tempfile.NamedTemporaryFile(suffix='.opus', delete=False)
                                        raw_opus_file.write(raw_data)
                                        raw_opus_file.close()
                                        raw_opus_path = raw_opus_file.name
                                        
                                        # Try opusdec with different skip values
                                        for skip_bytes in [0, 4, 8, 12, 16]:
                                            if skip_bytes >= len(raw_data):
                                                break
                                            
                                            test_data = raw_data[skip_bytes:]
                                            if len(test_data) < 10:
                                                continue
                                            
                                            with open(raw_opus_path, 'wb') as f:
                                                f.write(test_data)
                                            
                                            # opusdec reads from stdin or file, outputs WAV
                                            cmd = ["opusdec", "--force-wav", raw_opus_path, "-"]
                                            opusdec_result = subprocess.run(
                                                cmd,
                                                capture_output=True,
                                                timeout=10
                                            )
                                            
                                            if opusdec_result.returncode == 0 and len(opusdec_result.stdout) > 44:
                                                # Convert WAV to PCM (skip WAV header, 44 bytes)
                                                pcm_data = opusdec_result.stdout[44:]
                                                with open(temp_pcm, 'wb') as f:
                                                    f.write(pcm_data)
                                                log.info(f"✅ Successfully decoded with opusdec (skip {skip_bytes})")
                                                result.returncode = 0
                                                break
                                        
                                        try:
                                            os.unlink(raw_opus_path)
                                        except:
                                            pass
                                        
                                    except FileNotFoundError:
                                        log.debug("opusdec not found")
                                if result.returncode != 0:
                                    opusdec_path = shutil.which("opusdec")
                                    if opusdec_path:
                                        cmd = [
                                            opusdec_path,
                                            "--rate", str(SAMPLE_RATE),
                                            temp_opus,
                                            temp_pcm
                                        ]
                                        result = subprocess.run(
                                            cmd,
                                            capture_output=True,
                                            text=True,
                                            timeout=10
                                        )
                                        if result.returncode != 0:
                                            log.error(f"opusdec also failed: {result.stderr[:300]}")
                                            return
                                    else:
                                        log.error(f"All decode attempts failed. Raw data: {len(raw_data)} bytes, hex: {raw_data[:100].hex()}")
                                        log.error("Consider installing opus-tools (opusdec) or using opuslib")
                                        # Save raw data for analysis
                                        debug_dir = Path("debug")
                                        debug_dir.mkdir(exist_ok=True)
                                        raw_file = debug_dir / f"raw_opus_{int(time.time())}.bin"
                                        with open(raw_file, "wb") as f:
                                            f.write(raw_data)
                                        log.info(f"Saved raw Opus data to {raw_file} for analysis")
                                        return
                        
                        # Read decoded PCM
                        with open(temp_pcm, "rb") as f:
                            pcm_audio = f.read()
                        log.info(f"Decoded {len(pcm_audio)} bytes PCM from Opus stream using ffmpeg")
                    finally:
                        # Cleanup temp files
                        try:
                            os.unlink(temp_opus)
                            os.unlink(temp_pcm)
                        except:
                            pass
                else:
                    log.error("Cannot decode Opus: ffmpeg not available")
                    return
            except Exception as e:
                log.error(f"Failed to decode Opus stream: {e}", exc_info=True)
                return
        else:
            log.warning("Empty audio buffer")
            return
        
        # Save files for debugging
        debug_dir = Path(__file__).parent / "debug"
        debug_dir.mkdir(exist_ok=True)
        timestamp = int(time.time())
        
        # Save Opus if we have it
        if len(self.opus_buffer) > 0:
            opus_file = debug_dir / f"audio_{timestamp}.opus"
            opus_file.write_bytes(bytes(self.opus_buffer))
            log.info(f"Saved Opus to {opus_file} ({len(self.opus_buffer)} bytes)")
        
        # Save PCM
        pcm_file = debug_dir / f"audio_{timestamp}.pcm"
        pcm_file.write_bytes(pcm_audio)
        log.info(f"Saved PCM to {pcm_file} ({len(pcm_audio)} bytes)")
        
        log.info(f"Processing {len(pcm_audio)} bytes PCM")
        
        # Save raw Opus for debugging (only if we have opus_data)
        # Note: opus_data is only defined in the elif block, so check if it exists
        if 'opus_data' in locals() and opus_data:
            debug_dir = Path(__file__).parent / "debug"
            debug_dir.mkdir(exist_ok=True)
            opus_file = debug_dir / f"audio_{int(time.time())}.opus"
            opus_file.write_bytes(opus_data)
            log.info(f"Saved raw Opus to {opus_file}")
        
        # STT (для логирования)
        log.info("Sending audio to ElevenLabs STT...")
        transcript = await self.stt.transcribe(bytes(pcm_audio))
        if transcript:
            log.info(f"✅ TRANSCRIPT RECEIVED: {transcript}")
            
            # Save transcript to file for verification
            debug_dir = Path(__file__).parent / "debug"
            debug_dir.mkdir(exist_ok=True)
            transcript_timestamp = int(time.time())
            transcript_file = debug_dir / f"transcript_{transcript_timestamp}.txt"
            try:
                with open(transcript_file, 'w', encoding='utf-8') as f:
                    f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Audio file: audio_{transcript_timestamp}.pcm\n")
                    f.write(f"PCM size: {len(pcm_audio)} bytes\n")
                    f.write(f"\n--- TRANSCRIPT ---\n")
                    f.write(transcript)
                    f.write(f"\n--- END TRANSCRIPT ---\n")
                log.info(f"Transcript saved to: {transcript_file}")
                # Print transcript to console (avoid emoji encoding issues)
                print(f"\n{'='*60}")
                print(f"TRANSCRIPT SAVED TO FILE:")
                print(f"   {transcript_file}")
                print(f"   Content: {transcript}")
                print(f"{'='*60}\n")
            except Exception as e:
                log.error(f"Failed to save transcript: {e}")
        else:
            log.warning("No transcript from STT - empty response, but continuing anyway")
        
        # ── Clarity check ───────────────────────────────────────────────
        if transcript and transcript.strip():
            is_clear, clarity_reason = assess_clarity(transcript)
            if not is_clear:
                log.warning(f"🔇 Unclear transcript ({clarity_reason}): '{transcript}'")
                response_text = "Не расслышал. Повтори, пожалуйста, чуть чётче."
            else:
                # ── Отправляем в главный агент OpenClaw (полный доступ к инструментам) ─
                log.info(f"→ OpenClaw bridge: {transcript[:100]}")
                response_text = await self.bridge.chat(transcript)
                if not response_text:
                    response_text = "Прости, не смог ответить."
        else:
            response_text = "Не расслышал, повтори пожалуйста."

        log.info(f"Voice response ({len(response_text)} chars): {response_text[:120]}")
        
        # TTS
        response_audio = await self.tts.synthesize(response_text)
        if not response_audio:
            log.warning("No audio from TTS")
            return
            
        log.info(f"TTS generated {len(response_audio)} bytes (16kHz mono)")
        
        # Try different encoding strategies (report: "Zello works in Mono")
        # Strategy A: 8kHz MONO (no stereo) - may produce Config 0 and match incoming
        # Strategy B: 16kHz MONO - use env ZELLO_USE_16KHZ=1 to try
        # Strategy C: 20ms frames - use env ZELLO_PACKET_20MS=1 (packet_duration 20, frame 20ms)
        use_16khz = os.getenv("ZELLO_USE_16KHZ", "").strip().lower() in ("1", "true", "yes")
        use_20ms = os.getenv("ZELLO_PACKET_20MS", "").strip().lower() in ("1", "true", "yes")
        
        if use_16khz:
            # 16kHz mono: no downsampling, codec_header gD4BPA==
            ENCODE_SAMPLE_RATE = 16000
            frame_duration_ms = 20 if use_20ms else 60
            frame_size_samples = int(ENCODE_SAMPLE_RATE * frame_duration_ms / 1000)  # 320 or 960
            frame_size_bytes = frame_size_samples * 2
            self.opus_codec.init_encoder(stereo=False, sample_rate=ENCODE_SAMPLE_RATE)
            log.info(f"Using 16kHz MONO encoding, {frame_duration_ms}ms frames (ZELLO_USE_16KHZ=1)")
        else:
            # 8kHz MONO: downsample, no stereo (report says Zello uses mono)
            downsampled_audio = bytearray()
            for i in range(0, len(response_audio), 4):
                if i + 3 < len(response_audio):
                    downsampled_audio.extend(response_audio[i:i+2])
            response_audio = bytes(downsampled_audio)
            log.info(f"Downsampled to 8kHz mono: {len(response_audio)} bytes")
            
            ENCODE_SAMPLE_RATE = 8000
            frame_duration_ms = 20 if use_20ms else 60
            frame_size_samples = int(ENCODE_SAMPLE_RATE * frame_duration_ms / 1000)  # 160 or 480
            frame_size_bytes = frame_size_samples * 2
            self.opus_codec.init_encoder(stereo=False, sample_rate=ENCODE_SAMPLE_RATE)
            log.info(f"Using 8kHz MONO encoding, {frame_duration_ms}ms frames (default)")
        
        opus_packets = []
        
        for i in range(0, len(response_audio), frame_size_bytes):
            frame = response_audio[i:i+frame_size_bytes]
            if len(frame) > 0:
                # Pad frame if needed to match expected size
                if len(frame) < frame_size_bytes:
                    # Pad with zeros (silence)
                    frame = frame + b'\x00' * (frame_size_bytes - len(frame))
                
                # Encode with frame_size matching Zello's 60ms (480 samples PER CHANNEL at 8kHz)
                # For stereo: frame_size = 480 (samples per channel), NOT 960!
                # Buffer size is 1920 bytes (480 * 2 channels * 2 bytes), but frame_size is still 480
                opus_packet = self.opus_codec.encode(frame, frame_size=frame_size_samples)
                if opus_packet:
                    opus_packets.append(opus_packet)
                    # Detailed logging for first packet
                    if len(opus_packets) == 1:
                        toc_byte = opus_packet[0] if len(opus_packet) > 0 else 0
                        # Parse TOC byte (same as handle_binary_message): Config=0-2, Stereo=bit3, FrameCount=4-6
                        config_id = toc_byte & 0x07
                        stereo = (toc_byte >> 3) & 0x01
                        frame_count_code = (toc_byte >> 4) & 0x07
                        log.info(f"🔍 Encoded first Opus packet: {len(opus_packet)} bytes")
                        log.info(f"🔍 TOC byte: 0x{toc_byte:02x} (binary: {toc_byte:08b})")
                        log.info(f"🔍 TOC parsed: Config={config_id}, Stereo={stereo}, FrameCountCode={frame_count_code}")
                        log.info(f"🔍 First 20 bytes of encoded Opus: {opus_packet[:20].hex()}")
                        log.info(f"🔍 Compare with INCOMING: TOC=0x18 (Config=0, Stereo=1, FrameCount=1)")
                    log.debug(f"Encoded Opus packet {len(opus_packets)}: {len(opus_packet)} bytes")
        
        if len(opus_packets) == 0:
            log.warning("No Opus audio encoded")
            return
            
        total_bytes = sum(len(p) for p in opus_packets)
        log.info(f"Encoded {len(opus_packets)} Opus packets, {total_bytes} bytes total")
        
        # Send audio back to Zello (pass sample rate and frame duration for codec_header and pacing)
        await self.send_audio_stream(opus_packets, sample_rate=ENCODE_SAMPLE_RATE, packet_duration_ms=frame_duration_ms)
        
    async def send_audio_stream(self, opus_packets: list, sample_rate: int = 8000, packet_duration_ms: int = 60):
        """Send audio stream to Zello channel.
        
        Args:
            opus_packets: List of Opus packets, where each packet is one frame.
            sample_rate: Sample rate used for encoding (8000 or 16000). Used for codec_header.
            packet_duration_ms: Frame duration in ms (20, 40, or 60). Used for codec_header and pacing.
        
        According to technical report, Zello uses binary packets with 9-byte header:
        - Byte 0: Packet Type (0x01 for audio)
        - Bytes 1-4: Stream ID (uint32, Big Endian)
        - Bytes 5-8: Packet ID (uint32, Big Endian)
        - Bytes 9+: Opus payload (one complete Opus packet per binary packet)
        """
        if not self.ws:
            log.error("WebSocket not connected")
            return
            
        # Initialize encoder if needed (sample_rate already set by process_audio_stream)
        if not self.opus_codec.encoder:
            self.opus_codec.init_encoder(stereo=False, sample_rate=sample_rate)
        codec_header = self.opus_codec.get_codec_header(sample_rate=sample_rate, frame_duration_ms=packet_duration_ms)
        
        # Start stream via JSON command
        # According to on_stream_start format, we need: type, codec, codec_header, packet_duration
        start_seq = int(time.time() * 1000)
        self.pending_start_seq = start_seq
        self.outgoing_stream_id = None
        self.stream_start_event.clear()  # Reset event
        
        start_msg = {
            "command": "start_stream",
            "seq": start_seq,
            "channel": ZELLO_CHANNEL,
            "type": "audio",  # Required: audio stream type
            "codec": "opus",
            "codec_header": base64.b64encode(codec_header).decode("ascii"),
            "packet_duration": packet_duration_ms,  # 20, 40, or 60 ms
        }
        await self.ws.send(json.dumps(start_msg))
        log.info(f"Audio stream start command sent (seq={start_seq})")
        
        # Wait for confirmation from server (will be handled in handle_message via event)
        # Wait up to 10 seconds for stream_id (server may be slow)
        if self.stream_start_event is None:
            log.error("❌ stream_start_event is None, cannot wait for stream_id")
            return
            
        try:
            await asyncio.wait_for(self.stream_start_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("⚠️ No stream_id received after 5s — server did not confirm stream start")
            # Reset pending state and bail out cleanly
            self.pending_start_seq = None
            self.outgoing_stream_id = None
            return
        
        if self.outgoing_stream_id is None:
            log.error("❌ Stream start failed or stream_id is None, cannot send audio")
            return
        
        outgoing_stream_id = self.outgoing_stream_id
        log.info(f"✅ Using stream_id={outgoing_stream_id} for sending audio")
        
        # Wait a bit for stream to fully initialize
        await asyncio.sleep(0.1)
        
        # Send audio packets as binary with 9-byte header
        # According to technical report, Zello uses binary packets
        # Each Opus packet is one 60ms frame - send it as a separate binary packet
        packet_id = 0
        
        for opus_packet in opus_packets:
            if not opus_packet or len(opus_packet) == 0:
                continue
                
            # Create binary packet with 9-byte header
            # According to technical report: struct.pack('>BII', 1, stream_id, packet_id)
            # Byte 0: Packet Type (0x01 for audio) - Big Endian byte
            # Bytes 1-4: Stream ID (Big Endian uint32)
            # Bytes 5-8: Packet ID (Big Endian uint32)
            # Bytes 9+: Opus payload (complete Opus packet for one 60ms frame)
            header = struct.pack('>BII', 0x01, outgoing_stream_id, packet_id)
            binary_packet = header + opus_packet
            
            # Send as binary (as per technical report)
            await self.ws.send(bytes(binary_packet))
            
            if packet_id == 0 or packet_id % 5 == 0:  # Log first packet and every 5th
                log.info(f"📤 Sent binary packet {packet_id}: {len(binary_packet)} bytes (Opus: {len(opus_packet)} bytes, stream_id={outgoing_stream_id})")
                # Log first packet structure for debugging
                if packet_id == 0:
                    log.info(f"🔍 First outgoing packet hex: {binary_packet[:20].hex()}")
                    toc_byte = opus_packet[0] if len(opus_packet) > 0 else 0
                    log.info(f"🔍 First OUTGOING Opus TOC byte: 0x{toc_byte:02x} (binary: {toc_byte:08b})")
                    log.info(f"🔍 OUTGOING packet: stream_id={outgoing_stream_id}, packet_id={packet_id}, opus_len={len(opus_packet)}")
            
            packet_id += 1
            # Pacing: Send packets at real-time speed (delay = frame duration)
            await asyncio.sleep(packet_duration_ms / 1000.0)
        
        log.info(f"📤 Sent {packet_id} binary packets total, stream_id={outgoing_stream_id}")
                
        # Stop stream via JSON command
        # stop_stream requires stream_id parameter
        stop_seq = int(time.time() * 1000) + 1000
        stop_msg = {
            "command": "stop_stream",
            "seq": stop_seq,
            "stream_id": outgoing_stream_id,  # Required: stream_id from start_stream response
        }
        await self.ws.send(json.dumps(stop_msg))
        log.info(f"Audio stream stop command sent (stream_id={outgoing_stream_id})")
    
    async def _send_pending_audio(self, opus_packets: list, stream_id: int, packet_duration_ms: int = 60):
        """Send pending audio packets that were waiting for stream_id.
        
        Args:
            opus_packets: List of Opus packets, where each packet is one frame.
            stream_id: Stream ID received from server.
            packet_duration_ms: Frame duration in ms for pacing (20, 40, or 60).
        """
        log.info(f"📤 Starting to send pending audio with stream_id={stream_id}")
        
        await asyncio.sleep(0.1)
        
        packet_id = 0
        for opus_packet in opus_packets:
            if not opus_packet or len(opus_packet) == 0:
                continue
                
            # Create binary packet with 9-byte header
            # According to technical report: struct.pack('>BII', 1, stream_id, packet_id)
            header = struct.pack('>BII', 0x01, stream_id, packet_id)
            binary_packet = header + opus_packet
            
            # Send as binary (as per technical report)
            await self.ws.send(bytes(binary_packet))
            
            if packet_id == 0 or packet_id % 5 == 0:  # Log first packet and every 5th
                log.info(f"📤 Sent binary packet {packet_id}: {len(binary_packet)} bytes (Opus: {len(opus_packet)} bytes, stream_id={stream_id})")
                # Log first packet structure for debugging
                if packet_id == 0:
                    log.info(f"🔍 First outgoing packet hex: {binary_packet[:20].hex()}")
                    toc_byte = opus_packet[0] if len(opus_packet) > 0 else 0
                    log.info(f"🔍 First OUTGOING Opus TOC byte: 0x{toc_byte:02x} (binary: {toc_byte:08b})")
                    log.info(f"🔍 OUTGOING packet: stream_id={stream_id}, packet_id={packet_id}, opus_len={len(opus_packet)}")
            
            packet_id += 1
            await asyncio.sleep(packet_duration_ms / 1000.0)
        
        log.info(f"📤 Sent {packet_id} binary packets total, stream_id={stream_id}")
        
        await asyncio.sleep(0.2)
        
        if self.current_stream == stream_id:
            log.info(f"ℹ️ Stream {stream_id} was already stopped by server, skipping stop_stream command")
        else:
            # Stop stream via JSON command
            stop_seq = int(time.time() * 1000) + 1000
            stop_msg = {
                "command": "stop_stream",
                "seq": stop_seq,
                "stream_id": stream_id,
            }
            await self.ws.send(json.dumps(stop_msg))
            log.info(f"Audio stream stop command sent (stream_id={stream_id})")
        
    async def handle_text_message(self, text: str, from_user: str):
        """Handle text message and send text response."""
        response = await self.bridge.chat(text)
        
        text_msg = {
            "command": "send_text_message",
            "seq": int(time.time() * 1000),
            "channel": ZELLO_CHANNEL,
            "text": response,
        }
        await self.ws.send(json.dumps(text_msg))
        log.info(f"Sent text response: {response[:100]}...")
        
    async def _send_telegram(self, text: str):
        """Send message to Valery via Telegram bot."""
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id   = os.getenv("TELEGRAM_CHAT_ID", "170488995")
        if not bot_token:
            log.warning("TELEGRAM_BOT_TOKEN not set — skipping Telegram notification")
            return
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                })
                if resp.status_code == 200:
                    log.info("✅ Telegram message sent")
                else:
                    log.warning(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            log.error(f"Telegram error: {e}")

    async def close(self):
        """Close WebSocket connection."""
        if self.ws:
            await self.ws.close()

# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Notification Queue — внешние скрипты пишут в notify_queue.json,
# бот читает и отправляет голосом через уже открытое соединение
# ---------------------------------------------------------------------------

NOTIFY_QUEUE_FILE = Path(__file__).parent / "notify_queue.json"

async def notify_queue_worker(client: "ZelloClient"):
    """
    Фоновая задача: каждые 5 сек читает notify_queue.json,
    обрабатывает сообщения через TTS + send_audio_stream.
    """
    log.info("[NotifyQueue] Worker started, watching notify_queue.json")
    while True:
        try:
            await asyncio.sleep(5)
            if not NOTIFY_QUEUE_FILE.exists():
                continue
            try:
                content = NOTIFY_QUEUE_FILE.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                messages = json.loads(content)
            except Exception as e:
                log.error(f"[NotifyQueue] Parse error: {e}")
                NOTIFY_QUEUE_FILE.unlink(missing_ok=True)
                continue

            if not isinstance(messages, list) or not messages:
                continue

            # Берём первое сообщение
            item = messages[0]
            remaining = messages[1:]

            text = item if isinstance(item, str) else item.get("text", "")
            if not text:
                NOTIFY_QUEUE_FILE.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
                continue

            log.info(f"[NotifyQueue] Sending notification: {text[:80]}")

            # Проверяем соединение
            if not client.ws:
                log.warning("[NotifyQueue] WebSocket not ready, skipping")
                continue

            try:
                # TTS
                pcm = await client.tts.synthesize(text)
                if not pcm:
                    raise RuntimeError("TTS returned empty audio")

                # Encode PCM → Opus (используем тот же путь что и для ответов)
                import opuslib as _opuslib
                sample_rate = 16000
                frame_ms = 20
                frame_samples = sample_rate * frame_ms // 1000
                encoder = _opuslib.Encoder(sample_rate, 1, _opuslib.APPLICATION_VOIP)
                packets = []
                chunk = frame_samples * 2
                remainder = len(pcm) % chunk
                if remainder:
                    pcm += b'\x00' * (chunk - remainder)
                for i in range(0, len(pcm), chunk):
                    pkt = bytes(encoder.encode(pcm[i:i+chunk], frame_samples))
                    packets.append(pkt)

                await client.send_audio_stream(packets, sample_rate=sample_rate, packet_duration_ms=frame_ms)
                log.info(f"[NotifyQueue] Sent {len(packets)} packets OK")

            except Exception as e:
                log.error(f"[NotifyQueue] Send error: {e}")

            # Убираем обработанное, сохраняем остаток
            if remaining:
                NOTIFY_QUEUE_FILE.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
            else:
                NOTIFY_QUEUE_FILE.unlink(missing_ok=True)

        except asyncio.CancelledError:
            log.info("[NotifyQueue] Worker cancelled")
            break
        except Exception as e:
            log.error(f"[NotifyQueue] Unexpected error: {e}")


def add_to_notify_queue(text: str):
    """Добавить сообщение в очередь нотификаций (синхронный вызов из внешних скриптов)."""
    try:
        if NOTIFY_QUEUE_FILE.exists():
            existing = json.loads(NOTIFY_QUEUE_FILE.read_text(encoding="utf-8"))
        else:
            existing = []
        existing.append(text)
        NOTIFY_QUEUE_FILE.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        print(f"[NotifyQueue] Error adding to queue: {e}")
        return False


async def main():
    """Main entry point."""
    # Validate configuration
    if not ELEVENLABS_API_KEY:
        log.error("ELEVENLABS_API_KEY is required")
        return
        
    if not ZELLO_PASSWORD:
        log.error("ZELLO_PASSWORD is required")
        return
        
    log.info("Starting Zello PTT Skill...")
    log.info(f"Zello: {ZELLO_WS_URL}")
    log.info(f"Channel: {ZELLO_CHANNEL}")
    log.info(f"Claude Gateway: {CLAUDE_GATEWAY_URL}")
    
    client = ZelloClient()

    # Запускаем фоновый воркер очереди нотификаций
    queue_task = asyncio.create_task(notify_queue_worker(client))

    # Авто-реконнект: при любом обрыве/kicked — переподключаемся
    retry_delay = 5
    max_delay = 60
    try:
        while True:
            try:
                log.info(f"[Main] Connecting...")
                await client.run()
                log.info("[Main] run() returned cleanly — restarting in 5s")
            except KeyboardInterrupt:
                log.info("Shutting down (KeyboardInterrupt)")
                break
            except Exception as e:
                log.error(f"[Main] Unexpected error: {e}")
            # Переподключение
            log.info(f"[Main] Reconnecting in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)
            client = ZelloClient()  # свежий клиент
            # Перепривязываем воркер к новому клиенту
            queue_task.cancel()
            try:
                await queue_task
            except asyncio.CancelledError:
                pass
            queue_task = asyncio.create_task(notify_queue_worker(client))
    finally:
        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
