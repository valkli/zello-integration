# Финальные критические вопросы для исследования

## Текущая проблема
- ✅ Codec header правильный: `gD4BPA==`
- ✅ Бинарный заголовок правильный: `struct.pack('>BII', 0x01, stream_id, packet_id)`
- ✅ Структура пакета правильная: 9-байтовый заголовок + Opus payload
- ✅ Задержка правильная: 60ms между пакетами
- ✅ Кодирование в стерео (как входящий поток)
- ❌ **Config ID неправильный: входящий Config=0, исходящий Config=11**
- ❌ **Сервер останавливает поток сразу после первого пакета**

## Критический вопрос: Config ID в Opus TOC byte

**Проблема:** Opus encoder создает пакеты с Config ID = 11, но должен быть Config ID = 0.

**Входящий TOC:** `0x18` = `00011000`
- Config ID: 0 (bits 7-3)
- Stereo: 1 (bit 2)
- Frame Count: 1 (bits 1-0)

**Исходящий TOC:** `0x5c` = `01011100`
- Config ID: 11 (bits 7-3) - **НЕПРАВИЛЬНО!**
- Stereo: 1 (bit 2) - правильно
- Frame Count: 0 (bits 1-0) - неправильно

## Что нужно выяснить

### 1. Как opuslib определяет Config ID?
**Вопрос:** Какие параметры encoder влияют на Config ID в TOC byte?

**Текущие параметры:**
- Sample Rate: 16000 Hz
- Channels: 2 (stereo)
- Application: APPLICATION_VOIP
- Bitrate: 24000

**Гипотезы:**
- Может быть, Config ID зависит от sample rate и channels?
- Может быть, нужно использовать APPLICATION_AUDIO вместо APPLICATION_VOIP?
- Может быть, нужно использовать другие параметры encoder (complexity, signal, etc.)?

**Где искать:**
- RFC 6716 - таблица соответствия Config ID и параметров
- Документация opuslib - как Config ID определяется
- Эталонная реализация zellostream.py - какие параметры используются

### 2. Правильно ли мы передаем PCM данные в encoder?
**Вопрос:** Правильно ли мы конвертируем моно в стерео и передаем в encoder?

**Текущий подход:**
- TTS возвращает PCM 16-bit mono
- Конвертируем в стерео: дублируем каждый sample (L=R)
- Разбиваем на кадры: 3840 bytes (960 samples × 2 channels × 2 bytes)
- Передаем в encoder.encode(frame, frame_size=960)

**Гипотезы:**
- Может быть, нужно передавать frame_size=1920 для стерео (total samples)?
- Может быть, формат PCM данных неправильный (нужен interleaved stereo)?
- Может быть, нужно использовать numpy array вместо bytes?

**Где искать:**
- Документация opuslib - формат входных данных для стерео
- Примеры использования opuslib с стерео PCM

### 3. Эталонная реализация zellostream.py
**Вопрос:** Как именно zellostream.py кодирует Opus пакеты?

**Что нужно найти:**
- Точный код инициализации encoder
- Параметры encoder (sample rate, channels, application, bitrate, complexity, etc.)
- Как вызывается encode() метод
- Формат входных PCM данных
- Значение frame_size, передаваемое в encode()

**Где искать:**
- GitHub: поиск "zellostream.py zello"
- GitHub: поиск "zello channel api python send audio"
- Официальный репозиторий: https://github.com/zelloptt/zello-channel-api

### 4. RFC 6716 - таблица Config ID
**Вопрос:** Какая таблица соответствия Config ID и параметров encoder?

**Что нужно найти:**
- Таблица из RFC 6716, показывающая соответствие Config ID и параметров
- Какие параметры дают Config ID = 0?

**Где искать:**
- RFC 6716: https://tools.ietf.org/html/rfc6716
- Раздел про TOC byte и Config ID

### 5. Альтернативные библиотеки
**Вопрос:** Может быть, нужно использовать другую библиотеку для кодирования Opus?

**Варианты:**
- pyogg (но она для Ogg контейнера)
- pydub с ffmpeg (но это создает файлы, не пакеты)
- Напрямую через ctypes к libopus

**Где искать:**
- Сравнение библиотек для Opus в Python
- Примеры использования разных библиотек

## Приоритетные вопросы

1. **КРИТИЧЕСКИЙ:** Найти эталонную реализацию zellostream.py и сравнить параметры encoder
2. **КРИТИЧЕСКИЙ:** Проверить RFC 6716 - таблица Config ID и параметров encoder
3. **ВЫСОКИЙ:** Проверить документацию opuslib - правильное использование для стерео
4. **СРЕДНИЙ:** Проверить формат PCM данных для стерео (interleaved vs non-interleaved)

## Где искать ответы

1. **GitHub:**
   - https://github.com/zelloptt/zello-channel-api
   - Поиск "zellostream.py"
   - Поиск "zello python send audio opus"

2. **RFC 6716:**
   - https://tools.ietf.org/html/rfc6716
   - Раздел 3.1: TOC Byte
   - Таблица Config ID

3. **Документация opuslib:**
   - https://github.com/onbeep/opuslib
   - Примеры использования
   - API документация

4. **Примеры кода:**
   - Другие проекты, использующие opuslib для стерео кодирования
   - Discord bots, использующие Opus
