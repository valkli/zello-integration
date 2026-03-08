"""
Zello Command Dispatcher
========================
Реестр команд, которые бот может выполнить по голосовой инструкции.
Каждая команда — это локальный скрипт или действие на машине Валерия.
"""

import asyncio
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("zello-commands")

WORKSPACE = Path(r"C:\Users\Val\.openclaw\workspace")


# ---------------------------------------------------------------------------
# Tool definitions (для function calling в Claude API)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "publish_milanuncios",
            "description": (
                "Публикует следующий товар из очереди Notion на Milanuncios. "
                "Используй когда пользователь просит опубликовать объявление, "
                "товар, публикацию на Milanuncios / Миланунсиос."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "True если запрос понятен и однозначен"
                    }
                },
                "required": ["confirm"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_queue_milanuncios",
            "description": (
                "Показывает сколько товаров осталось в очереди Milanuncios. "
                "Используй когда пользователь спрашивает про очередь, "
                "сколько осталось товаров, статус очереди."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_idealista_sync",
            "description": (
                "Запускает синхронизацию агентства недвижимости с Idealista. "
                "Используй когда просят запустить синхронизацию Idealista, "
                "обновить объекты недвижимости."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            }
        }
    },
]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class CommandExecutor:
    """Выполняет команды, вызванные LLM через function calling."""

    def __init__(self):
        self.workspace = WORKSPACE

    async def execute(self, tool_name: str, tool_args: dict) -> str:
        """Выполнить команду и вернуть голосовой ответ."""
        log.info(f"⚡ Executing tool: {tool_name}({tool_args})")

        if tool_name == "publish_milanuncios":
            return await self._publish_milanuncios()
        elif tool_name == "check_queue_milanuncios":
            return await self._check_queue_milanuncios()
        elif tool_name == "run_idealista_sync":
            return await self._run_idealista_sync()
        else:
            return f"Не знаю команду {tool_name}."

    # Cron job ID for Milanuncios daily poster
    MILANUNCIOS_CRON_ID = "211a6e98-a3d3-4c88-913f-8e51fc1226f1"

    async def _publish_milanuncios(self) -> str:
        """Запускает публикацию через OpenClaw cron job (полный цикл с браузером)."""
        try:
            log.info(f"Triggering Milanuncios cron job: {self.MILANUNCIOS_CRON_ID}")
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "cron", "run", self.MILANUNCIOS_CRON_ID,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Не ждём завершения (публикация занимает 5-20 мин)
            # Ждём только старта — 10 секунд
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
                output = (stdout or b"").decode("utf-8", errors="replace").strip()
                if proc.returncode == 0 or "started" in output.lower() or "running" in output.lower():
                    return "Запустил публикацию на Milanuncios. Процесс займёт несколько минут, результат придёт в Telegram."
                else:
                    log.warning(f"Cron run output: {output[:200]}")
                    return "Публикация запущена. Проверь через несколько минут."
            except asyncio.TimeoutError:
                # Если не ответил за 10 сек — значит запустился и работает в фоне
                return "Публикация запущена в фоне. Результат придёт в Telegram через несколько минут."

        except FileNotFoundError:
            # openclaw не в PATH — попробуем через node
            log.warning("openclaw not in PATH, trying npx/node")
            return "Не нашёл команду openclaw. Запусти публикацию вручную из Telegram."
        except Exception as e:
            log.error(f"Publish command error: {e}")
            return "Произошла ошибка при запуске публикации."

    async def _check_queue_milanuncios(self) -> str:
        """Показывает статус очереди."""
        try:
            # Попробуем запросить из Notion через существующий скрипт
            script = self.workspace / "milanuncios-poster" / "fetch_product_for_milanuncios.py"
            if script.exists():
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(script), "--count-only",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(script.parent),
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                output = stdout.decode("utf-8", errors="replace").strip()
                if output and output.isdigit():
                    count = int(output)
                    return f"В очереди Milanuncios осталось {count} товаров."
            return "Не смог получить количество товаров из очереди."
        except Exception as e:
            log.error(f"Queue check error: {e}")
            return "Ошибка при проверке очереди."

    async def _run_idealista_sync(self) -> str:
        """Запускает синхронизацию Idealista."""
        script = self.workspace / "idealista-notion-sync" / "master_scheduler.py"
        if not script.exists():
            return "Скрипт синхронизации Idealista не найден."
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script), "--run-now",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(script.parent),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode == 0:
                return "Синхронизация Idealista запущена."
            else:
                return "Ошибка при запуске синхронизации Idealista."
        except asyncio.TimeoutError:
            return "Синхронизация запущена в фоне."
        except Exception as e:
            log.error(f"Idealista sync error: {e}")
            return "Не удалось запустить синхронизацию."


# ---------------------------------------------------------------------------
# Clarity checker
# ---------------------------------------------------------------------------

def assess_clarity(transcript: str) -> tuple[bool, str]:
    """
    Проверяет насколько чёткий транскрипт.
    
    Returns:
        (is_clear, reason) — True если запрос понятен, False если нужно переспросить
    """
    if not transcript or not transcript.strip():
        return False, "Пустой транскрипт"

    text = transcript.strip()
    words = text.split()

    # Слишком короткий (1 буква — точно шум/случайный нажим)
    if len(words) == 1 and len(text) <= 1:
        return False, f"Очень короткий: '{text}'"

    # Явный мусор — одни знаки препинания или числа без контекста
    import re
    if re.match(r'^[\d\s\.\,\!\?\-]+$', text):
        return False, f"Только цифры/знаки: '{text}'"

    # Повторяющиеся слова (стаккато STT)
    if len(words) >= 3:
        unique_words = set(w.lower() for w in words)
        if len(unique_words) == 1:
            return False, f"Повторяющийся звук: '{text}'"

    # Слишком короткий для команды (< 2 слов) но не пустой
    if len(words) < 2:
        # Может быть односложная команда — оставляем
        return True, "Короткое но допустимое"

    return True, "Нормальный транскрипт"
