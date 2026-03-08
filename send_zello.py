"""
send_zello.py — Отправка голосового уведомления в Zello.

Использование:
    python send_zello.py "Текст уведомления"
    python send_zello.py --text "Текст" --channel testvaleryklintsou
    python send_zello.py --file message.txt

Также доступен как Python модуль:
    from send_zello import send_voice
    await send_voice("Синк завершён. 3 новых объекта.")
"""
import asyncio
import argparse
import json
import os
import struct
import base64
import time
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
import websockets

load_dotenv(Path(__file__).parent / ".env")

WS_URL     = os.getenv("ZELLO_WS_URL",    "wss://zello.io/ws")
USERNAME   = os.getenv("ZELLO_USERNAME",  "")
PASSWORD   = os.getenv("ZELLO_PASSWORD",  "")
AUTH_TOKEN = os.getenv("ZELLO_AUTH_TOKEN","")
CHANNEL    = os.getenv("ZELLO_CHANNEL",   "testvaleryklintsou")
XI_KEY     = os.getenv("ELEVENLABS_API_KEY", "")
VOICE_ID   = os.getenv("ELEVENLABS_VOICE_ID","21m00Tcm4TlvDq8ikWAM")

SAMPLE_RATE   = 16000
FRAME_MS      = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320

# === LOCK: предотвращает двойную отправку в течение 120 сек ===
_LOCK_FILE = Path(__file__).parent / ".send_lock"
_LOCK_TTL  = 120  # секунд

def _acquire_lock() -> bool:
    """Возвращает True если можно отправлять, False если уже отправляется."""
    if _LOCK_FILE.exists():
        age = time.time() - _LOCK_FILE.stat().st_mtime
        if age < _LOCK_TTL:
            print(f"[Zello] SKIP — уже отправляется (lock age={age:.0f}s < {_LOCK_TTL}s). Повтор заблокирован.")
            return False
    _LOCK_FILE.write_text(str(os.getpid()))
    return True

def _release_lock():
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass
# =================================================================


async def tts(text: str) -> bytes:
    """Синтез речи через ElevenLabs → PCM 16kHz mono."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream?output_format=pcm_16000"
    headers = {"xi-api-key": XI_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 200:
            if resp.content[:3] == b'ID3':
                raise RuntimeError("ElevenLabs вернул MP3 вместо PCM — проверь output_format")
            return resp.content
        raise RuntimeError(f"TTS error {resp.status_code}: {resp.text[:100]}")


def encode_opus(pcm: bytes) -> list:
    """PCM 16kHz mono → список Opus пакетов."""
    try:
        import opuslib
    except ImportError:
        raise RuntimeError("opuslib не установлен")

    encoder = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
    packets = []
    chunk = FRAME_SAMPLES * 2
    remainder = len(pcm) % chunk
    if remainder:
        pcm += b'\x00' * (chunk - remainder)
    for i in range(0, len(pcm), chunk):
        frame = pcm[i:i+chunk]
        packets.append(bytes(encoder.encode(frame, FRAME_SAMPLES)))
    return packets


async def _send_voice_impl(text: str, channel: str = None) -> bool:
    """Внутренняя реализация отправки (вызывается только при наличии lock)."""
    ch = channel or CHANNEL
    print(f"[Zello] Отправляю в канал '{ch}': {text[:80]}...")

    # 1. TTS
    try:
        pcm = await tts(text)
    except Exception as e:
        print(f"[Zello] TTS ошибка: {e}")
        return False

    # 2. Opus
    try:
        packets = encode_opus(pcm)
    except Exception as e:
        print(f"[Zello] Opus ошибка: {e}")
        return False

    # 3. Zello WebSocket
    try:
        async with websockets.connect(WS_URL) as ws:
            # Логон
            await ws.send(json.dumps({
                "command": "logon", "seq": 1,
                "auth_token": AUTH_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "channels": [ch],
            }))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if not resp.get("success"):
                print(f"[Zello] Логон не удался: {resp}")
                return False

            # Ждём channel status
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                status_data = json.loads(msg)
                status = status_data.get("status", "?")
                users = status_data.get("users_online", 0)
                print(f"[Zello] Канал: {status}, пользователей: {users}")
            except asyncio.TimeoutError:
                pass

            await asyncio.sleep(0.3)

            # start_stream
            codec_header = struct.pack('<HBB', SAMPLE_RATE, 1, FRAME_MS)
            seq = int(time.time() * 1000)
            await ws.send(json.dumps({
                "command": "start_stream", "seq": seq,
                "channel": ch, "type": "audio", "codec": "opus",
                "codec_header": base64.b64encode(codec_header).decode(),
                "packet_duration": FRAME_MS,
            }))

            # Ждём stream_id
            stream_id = None
            deadline = time.time() + 5.0
            while time.time() < deadline:
                try:
                    remaining = deadline - time.time()
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining)))
                    if "stream_id" in msg:
                        stream_id = msg["stream_id"]
                        break
                    print(f"[Zello] Пропускаю: {msg.get('command', msg)}")
                except asyncio.TimeoutError:
                    break

            if not stream_id:
                print("[Zello] Нет stream_id после 5 сек — stream не открылся")
                return False

            # Отправляем пакеты
            for i, pkt in enumerate(packets):
                header = struct.pack('>BII', 0x01, stream_id, i)
                await ws.send(header + pkt)
                await asyncio.sleep(FRAME_MS / 1000.0)

            # stop_stream
            await ws.send(json.dumps({
                "command": "stop_stream",
                "seq": seq + 1,
                "stream_id": stream_id,
            }))
            await asyncio.sleep(0.5)
            print(f"[Zello] ✅ Отправлено {len(packets)} пакетов → {ch}")
            return True

    except Exception as e:
        print(f"[Zello] Ошибка: {e}")
        return False


async def send_voice(text: str, channel: str = None) -> bool:
    """
    Главная функция — отправляет голосовое сообщение в Zello.
    Защищена от двойного вызова через lock-файл (TTL=120s).
    Lock снимается ТОЛЬКО при успехе — при ошибке блокирует повторы на 120 сек.
    Возвращает True если успешно (или если заблокировано — чтобы агент не ретраил).
    """
    if not _acquire_lock():
        return True  # возвращаем True чтобы агент не пытался повторить

    result = await _send_voice_impl(text, channel)
    if result:
        _release_lock()  # снимаем lock ТОЛЬКО при успехе
    # При ошибке lock остаётся — истечёт сам через 120 сек
    return result


def main():
    parser = argparse.ArgumentParser(description="Отправить голосовое в Zello")
    parser.add_argument("text", nargs="?", help="Текст сообщения")
    parser.add_argument("--text", "-t", dest="text_flag", help="Текст сообщения")
    parser.add_argument("--file", "-f", help="Файл с текстом")
    parser.add_argument("--channel", "-c", default=None, help=f"Канал (по умолчанию: {CHANNEL})")
    args = parser.parse_args()

    # Определяем текст
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8").strip()
    elif args.text_flag:
        text = args.text_flag
    elif args.text:
        text = args.text
    else:
        print("Укажи текст: python send_zello.py 'Текст уведомления'")
        sys.exit(1)

    ok = asyncio.run(send_voice(text, args.channel))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
