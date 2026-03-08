"""
Тест отправки голосового сообщения в Zello.
Ждём channel status=online перед отправкой.
"""
import asyncio
import json
import os
import struct
import base64
import time
import httpx
from pathlib import Path
from dotenv import load_dotenv
import websockets

load_dotenv(Path(__file__).parent / ".env")

WS_URL     = os.getenv("ZELLO_WS_URL",    "wss://zello.io/ws")
USERNAME   = os.getenv("ZELLO_USERNAME",  "valeryklintsou")
PASSWORD   = os.getenv("ZELLO_PASSWORD",  "")
AUTH_TOKEN = os.getenv("ZELLO_AUTH_TOKEN","")
XI_KEY     = os.getenv("ELEVENLABS_API_KEY", "")
VOICE_ID   = os.getenv("ELEVENLABS_VOICE_ID","21m00Tcm4TlvDq8ikWAM")

SAMPLE_RATE  = 8000
FRAME_MS     = 60
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480


async def get_tts_pcm(text: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream"
    headers = {"xi-api-key": XI_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        "output_format": "pcm_8000",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 200:
            print(f"✅ TTS: {len(resp.content)} байт PCM")
            return resp.content
        else:
            print(f"❌ TTS {resp.status_code}: {resp.text[:200]}")
            return b""


def pcm_to_opus_packets(pcm_bytes: bytes) -> list:
    import opuslib
    encoder = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
    packets = []
    chunk = FRAME_SAMPLES * 2
    remainder = len(pcm_bytes) % chunk
    if remainder:
        pcm_bytes += b'\x00' * (chunk - remainder)
    for i in range(0, len(pcm_bytes), chunk):
        frame = pcm_bytes[i:i+chunk]
        try:
            packets.append(bytes(encoder.encode(frame, FRAME_SAMPLES)))
        except Exception as e:
            print(f"⚠️ encode: {e}")
    print(f"✅ {len(packets)} Opus пакетов")
    return packets


async def send_to_channel(channel: str, opus_packets: list):
    print(f"\n{'='*50}")
    print(f"Подключаемся к каналу: {channel}")
    try:
        async with websockets.connect(WS_URL) as ws:
            # Логон
            await ws.send(json.dumps({
                "command": "logon", "seq": 1,
                "auth_token": AUTH_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "channels": [channel],
            }))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if not resp.get("success"):
                print(f"❌ Логон: {resp}")
                return False
            print(f"✅ Логон ОК")

            # Ждём on_channel_status
            channel_online = False
            for _ in range(5):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    data = json.loads(msg)
                    cmd = data.get("command","")
                    print(f"📨 {cmd}: {json.dumps(data, ensure_ascii=False)[:150]}")
                    if cmd == "on_channel_status":
                        status = data.get("status","")
                        users = data.get("users_online", 0)
                        print(f"   → status={status}, users_online={users}")
                        if status == "online":
                            channel_online = True
                            break
                        elif status == "offline":
                            print(f"   ⚠️ Канал оффлайн — всё равно попробуем отправить")
                            channel_online = True  # попробуем даже если оффлайн
                            break
                except asyncio.TimeoutError:
                    break

            # Небольшая пауза
            await asyncio.sleep(0.5)

            # start_stream
            seq = int(time.time() * 1000)
            codec_header = struct.pack('<HBB', SAMPLE_RATE, 1, FRAME_MS)
            await ws.send(json.dumps({
                "command": "start_stream",
                "seq": seq,
                "channel": channel,
                "type": "audio",
                "codec": "opus",
                "codec_header": base64.b64encode(codec_header).decode(),
                "packet_duration": FRAME_MS,
            }))
            print(f"📤 start_stream → канал {channel}")

            try:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                print(f"📨 start_stream resp: {resp}")
                stream_id = resp.get("stream_id")
            except asyncio.TimeoutError:
                print("❌ Нет ответа на start_stream")
                return False

            if not stream_id:
                print(f"❌ Нет stream_id: {resp}")
                return False

            print(f"✅ stream_id={stream_id}, шлём {len(opus_packets)} пакетов...")
            for i, pkt in enumerate(opus_packets):
                header = struct.pack('>BII', 0x01, stream_id, i)
                await ws.send(header + pkt)
                await asyncio.sleep(FRAME_MS / 1000.0)

            # stop_stream
            await ws.send(json.dumps({
                "command": "stop_stream",
                "seq": seq+1,
                "stream_id": stream_id,
            }))
            print(f"📤 stop_stream отправлен")
            await asyncio.sleep(1.0)
            return True

    except Exception as e:
        print(f"❌ {e}")
        import traceback; traceback.print_exc()
        return False


async def main():
    text = "Привет! Это тест голосового сообщения от Али. Всё работает!"
    print(f"TTS: '{text}'")
    pcm = await get_tts_pcm(text)
    if not pcm:
        return
    opus_packets = pcm_to_opus_packets(pcm)
    if not opus_packets:
        return

    # Пробуем разные каналы
    channels_to_try = [
        "testvaleryklintsou",   # канал созданный в аккаунте valeryklintsou
        "Testvalstekli",         # канал созданный в аккаунте valstekli
        "valstekli",             # личный канал valstekli
    ]
    for ch in channels_to_try:
        ok = await send_to_channel(ch, opus_packets)
        if ok:
            print(f"\n🎉 Успешно отправлено в канал '{ch}'!")
            break
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
