# Zello Integration — Project Memory

**Статус:** ACTIVE — бот онлайн, auto-reconnect работает
**Последнее обновление:** 2026-03-10

---

## Текущее состояние
- Бот: аккаунт `valeryklintsou`, канал `testvaleryklintsou`
- Процесс: `python zello_skill.py` (2 экземпляра PID — watchdog запускает)
- Watchdog: `OpenClaw_ZelloSkill` в Планировщике задач (каждые 5 мин)
- Users online: 2

## Уведомления (КРИТИЧНО)
**Использовать ТОЛЬКО очередь — никогда send_zello.py напрямую!**

```python
# Правильный способ отправки:
import json, pathlib
q = pathlib.Path(r'C:\Users\Val\.openclaw\skills\zello\notify_queue.json')
msgs = json.loads(q.read_text('utf-8')) if q.exists() else []
msgs.append({'text': 'Сообщение здесь'})
q.write_text(json.dumps(msgs, ensure_ascii=False), 'utf-8')
```

Почему: только 1 WebSocket соединение на аккаунт. Второе — кикает бота.

## Ключевые файлы
- `C:\Users\Val\.openclaw\skills\zello\zello_skill.py` — основной бот
- `C:\Users\Val\.openclaw\skills\zello\notify_queue.json` — очередь уведомлений
- `C:\Users\Val\.openclaw\skills\zello\watchdog.ps1` — watchdog скрипт
- `C:\Users\Val\.openclaw\skills\zello\zello_out.log` — stdout лог
- `C:\Users\Val\.openclaw\skills\zello\zello_err.log` — stderr лог (основной)

## Логи и диагностика
```powershell
# Проверить процесс:
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*zello_skill*" }
# Лог:
Get-Content C:\Users\Val\.openclaw\skills\zello\zello_err.log -Tail 20
```

## Auto-reconnect
Добавлен в сессии 07.03: при kicked/обрыве — exponential backoff 5→60 сек, бесконечный retry.

## Сделано в сессии 07-09.03.2026
- ✅ Проверен статус (channel online, users=2)
- ✅ Отправлен тест-сигнал: «Тест. Зелло работает нормально.» — 98 пакетов OK
- Twitter notifications: короткие «Твит опубликован. День X, слот Y.»

## Интеграции использующие Zello
- Twitter Campaign — после каждого поста
- Milanuncios — итоговый отчёт за день
- Любые другие — через notify_queue.json

## Связанные файлы в папке
- `README.md` — документация
- `SKILL.md` — описание скилла
- `ZELLO_SKILL_PROMPT.md` — промпт для агента
