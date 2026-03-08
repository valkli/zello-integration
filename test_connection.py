"""
Тест подключения к Zello Consumer API — исправленная версия.
Ключевые находки из официальной документации:
- Параметр: "channels" (массив), а НЕ "channel" (строка)
- Named account: нужны auth_token + username + password вместе
- Direct message: параметр "for" в start_stream
"""
import asyncio
import json
import os
import base64
from pathlib import Path
from dotenv import load_dotenv
import websockets

load_dotenv(Path(__file__).parent / ".env")

WS_URL    = os.getenv("ZELLO_WS_URL",    "wss://zello.io/ws")
USERNAME  = os.getenv("ZELLO_USERNAME",  "")
PASSWORD  = os.getenv("ZELLO_PASSWORD",  "")
AUTH_TOKEN = os.getenv("ZELLO_AUTH_TOKEN", "")
CHANNEL   = os.getenv("ZELLO_CHANNEL",   "ali-assistant")


def decode_jwt_payload(token):
    try:
        parts = token.split(".")
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:
        return {"error": str(e)}


async def listen(ws, seconds=5):
    """Слушаем сообщения N секунд."""
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            if isinstance(msg, bytes):
                print(f"  📦 Binary: {len(msg)} байт")
            else:
                data = json.loads(msg)
                print(f"  📨 {json.dumps(data, ensure_ascii=False)}")
                cmd = data.get("command", "")
                if cmd == "on_logon_result":
                    return data.get("success", False) or data.get("status") == "ok"
                # Новый формат: {"seq": 1, "success": true, "refresh_token": "..."}
                if data.get("success") is True and data.get("seq") == 1:
                    print("  ✅ Логон УСПЕШЕН!")
                    return True
                if "error" in data:
                    print(f"  ❌ Ошибка: {data['error']}")
                    return False
        except asyncio.TimeoutError:
            break
    return None


async def test():
    payload = decode_jwt_payload(AUTH_TOKEN)
    print(f"JWT: {json.dumps(payload, ensure_ascii=False)}")
    print(f"URL: {WS_URL}, User: {USERNAME}")

    # === Вариант 1: channels=[] (массив!) + auth_token + username + password ===
    print("\n=== Вариант 1: channels (массив) + auth_token + username + password ===")
    try:
        async with websockets.connect(WS_URL) as ws:
            msg = {
                "command": "logon", "seq": 1,
                "auth_token": AUTH_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "channels": [CHANNEL],
            }
            print(f"📤 {json.dumps({**msg, 'auth_token': msg['auth_token'][:30]+'...'})}")
            await ws.send(json.dumps(msg))
            result = await listen(ws, 5)
            print(f"Результат: {result}")
    except Exception as e:
        print(f"❌ {e}")

    # === Вариант 2: channels=["valstekli"] — личный канал другого юзера ===
    print("\n=== Вариант 2: channels=['valstekli'] (личный канал получателя) ===")
    try:
        async with websockets.connect(WS_URL) as ws:
            msg = {
                "command": "logon", "seq": 1,
                "auth_token": AUTH_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "channels": ["valstekli"],
            }
            print(f"📤 {json.dumps({**msg, 'auth_token': '...'})}")
            await ws.send(json.dumps(msg))
            result = await listen(ws, 5)
            print(f"Результат: {result}")
    except Exception as e:
        print(f"❌ {e}")

    # === Вариант 3: listen_only на канале valstekli ===
    print("\n=== Вариант 3: listen_only + channels=['valstekli'] ===")
    try:
        async with websockets.connect(WS_URL) as ws:
            msg = {
                "command": "logon", "seq": 1,
                "auth_token": AUTH_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "channels": ["valstekli"],
                "listen_only": True,
            }
            await ws.send(json.dumps(msg))
            result = await listen(ws, 5)
            print(f"Результат: {result}")
    except Exception as e:
        print(f"❌ {e}")


if __name__ == "__main__":
    asyncio.run(test())
