# Аудит Phase 1: claude-phone (2026-11-03)

Подробности аудита из [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md), раздел 2 (там —
вывод и решение D1). Перенесено сюда без изменений, чтобы план оставался коротким.

### 2.1 Что осмотрено

| Источник | Как | Результат |
| --- | --- | --- |
| `claude-phone-main.zip` (загружен владельцем) | распакован; прочитаны модули, шаблоны Asterisk, документация, тесты | см. ниже |
| GitHub `risingpower/claude-phone` (публичный, MIT) | через подключённую интеграцию GitHub: дерево файлов, коммиты, issues, PR | ветка `main`, HEAD `28efe1d` от 2026-10-23 |
| Совпадение zip и GitHub | сравнение git blob/tree SHA: 5 корневых файлов и 5 поддеревьев (`asterisk`, `claude_phone`, `docs`, `scripts`, `tests`) | идентичны: zip = `main@28efe1d` |
| GitHub-аккаунт владельца `snail9847-coder` | список репозиториев (16) | репозитория wewln-phonecaller и форка claude-phone не было |

### 2.2 Что это за проект

claude-phone — локальная мини-АТС на одном Mac. Asterisk 22 (собирается из исходников) соединяет
**собственный настольный SIP-телефон владельца** (Yealink T31P или Linphone в той же локальной сети)
с сессиями Claude Code в iTerm2. Claude звонит владельцу через MCP-инструменты `call_<owner>` и
`notify_<owner>`, владелец звонит сессиям по внутренним номерам 301–399; есть голосовая почта,
оператор «0», игра Crack the Safe и управление лампами Govee. README прямо пишет: «Mac only (Apple Silicon)».

- Пакет `claude_phone/`: 22 файла, около 3 590 строк Python; 8 шаблонов Asterisk; 3 документа;
  8 файлов тестов (около 600 строк, 51 тест). Лицензия MIT © 2026 risingpower.
- Зависимости (`pyproject.toml`): Python ≥ 3.12; `aiohttp>=3.9`, `mcp>=2.3,<3`,
  `pyobjc-framework-EventKit>=10`; dev: `pytest>=8`.
- Внешние компоненты: Asterisk 22.11 (`scripts/build-asterisk.sh` с патчами под macOS и Homebrew),
  whisper.cpp `whisper-server`, macOS `say` и `afconvert`; опционально ElevenLabs, ntfy.sh, Govee LAN API.
- Issues/PR upstream: 1 открытый issue (совместимость с Yealink T26P), открытый PR #1 (голоса Siri),
  закрытый без слияния PR #3 «Run on Windows (WSL 2) and with analog phones» (643+/88−, 18 файлов, 2026-10-23).
  PR #3 не входит в zip и не прошёл ревью.

### 2.3 Проверки, выполненные на самом деле

| # | Действие | Среда | Результат |
| --- | --- | --- | --- |
| 1 | `pip install -e '.[dev]'` (как в README) | Linux, Python 3.13.15 | ошибка: `PyObjC requires macOS to build` |
| 2 | `aiohttp`, `mcp`, `pytest` без pyobjc, затем `pip install --no-deps -e .` | то же | успешно: aiohttp 3.14.5, mcp 2.3.0, pytest 9.2.0 |
| 3 | `python -m pytest -q` | то же | `51 passed in 0.88s` |
| 4 | сравнение SHA zip и GitHub | — | совпадают |

Оговорки: тесты работают с фейковым звонком и не задействуют Asterisk, whisper, `say` и iTerm2,
поэтому macOS-части ими не проверены. На Windows установка не запускалась; ожидается та же ошибка
PyObjC, так как это библиотека только для macOS **[не проверено на Windows]**.

### 2.4 Несовместимые допущения

#### A. Звонит только собственному SIP-телефону (выхода в телефонную сеть нет)

- `claude_phone/daemon.py:591` — единственный исходящий вызов: `POST /channels` с
  `endpoint=f"PJSIP/{ring_ext}"`, то есть внутренний номер 200 или 201.
- `asterisk/pjsip.conf.tmpl` — только LAN-эндпоинты 200/201 и ACL `deny=0.0.0.0/0` +
  `permit=@@LAN_CIDR@@`; нет SIP-транка, регистрации у оператора и outbound proxy.
- `asterisk/extensions.conf.tmpl` — единственный контекст `from-desk`, всё уходит в Stasis.
- Нет E.164, проверки номеров и настоящего caller ID (вместо него подпись `repo/branch`).

#### B. Привязка к macOS

| Где | Что | Чем заменить в wewln-phonecaller |
| --- | --- | --- |
| `inject.py` | AppleScript (`osascript`) → iTerm2 | не нужно, вне задачи |
| `tts.py:25`, `tts.py:36` | `afconvert`, `say` | кроссплатформенный TTS-адаптер и ресемплинг в Python |
| `keychain.py` | `security` (Keychain) | переменные окружения и `.env`; опционально Windows Credential Manager через `keyring` |
| `cli.py:112–235` | launchd/`launchctl`, `~/Library/LaunchAgents`, пути `/opt/homebrew` | `python -m wewln_phonecaller serve`, PowerShell-скрипты |
| `config.py:127–141` | `route -n get default`, `ipconfig getifaddr`, `ifconfig` | не нужно (нет телефона в LAN) |
| `diary.py`, `pyproject.toml` | EventKit через PyObjC | не нужно; блокирует установку вне macOS |
| `scripts/build-asterisk.sh`, `docs/build-notes.md` | сборка под `aarch64-apple-darwin`, openssl из Homebrew | если понадобится Asterisk — Docker или WSL 2 |
| `config.py:19` | `ASTERISK_RUN=/tmp/cphone` (лимит длины unix-сокета macOS) | не нужно |

#### C. Остальные расхождения с требованиями

- Одна линия: `daemon.py:93` — `self.line = asyncio.Lock()` («one call with the owner at a time»).
  Нет кампаний, очередей, конкурентности, расписаний и повторов.
- Только английский: `whisper-server ... -l en` (`cli.py:113`), фильтр «шумовых» фраз STT на английском,
  британские голоса.
- Нет Notion, контактов, согласий, DNC/suppression, часов звонков, лимитов частоты, kill switch,
  разделения симуляции и продакшна.
- HTTP API демона (`127.0.0.1:8765`: `/call`, `/notify`, `/transfer` …) без аутентификации —
  защищён только тем, что слушает localhost.
- Логи — обычный текст (`log.py`) с транскриптами; нет структурированных логов и correlation ID.
  Записи и кэш речи удаляются через 30 дней (`housekeeping.py`), журнал инъекций в БД не чистится.
- Аудио рассчитано на LAN (G.722, 16 кГц); в телефонной сети обычно G.711 8 кГц (μ-law/A-law).
  Модуль `audioop` удалён из Python 3.13, поэтому конвертацию надо делать без него.
- Asterisk нативно на Windows не работает — только через WSL 2 или Docker
  **[общеизвестно, в этом проекте не проверялось]**.

### 2.5 Что можно переиспользовать (с сохранением уведомления MIT)

| Модуль | Что полезно | Где пригодится |
| --- | --- | --- |
| `ari.py` (259 строк) | async-клиент Asterisk ARI: REST + WebSocket событий с переподключением, `Hangup`, play/record/DTMF | опциональный адаптер «свой Asterisk + SIP-транк» (только Docker/WSL 2) |
| `stt.py` | клиент `whisper-server` (`/inference`), грубый VAD по RMS, отсев «шумовых» транскриптов | STT-адаптер whisper.cpp |
| `tts.py` | интерфейс `Provider`, кэш по хэшу текста, запасной голос вместо тишины | интерфейс и кэш TTS |
| `registry.py` | работа с SQLite | паттерны хранилища |
| `tests/` | `FakeCall`, изолированный HOME через переменную окружения | тесты без реальных звонков |

Не переносим: `inject.py`, `hooks.py`, `mcp_server.py`, `diary.py`, `lights.py`, `safe.py`,
`screen.py`, `hold.py`, `operator.py`, `push.py`, launchd-часть `cli.py`, документацию по Yealink.
