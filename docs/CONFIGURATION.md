# Настройки

Все настройки задаются переменными окружения с префиксом `WEWLN_` или в файле `.env`.
Пример со всеми переменными — [`.env.example`](../.env.example).

## Порядок приоритета

1. Аргументы командной строки (`serve --host/--port`).
2. Переменные окружения.
3. Файл `.env` в папке запуска (UTF-8). Другой файл: `--env-file путь`; `--env-file ""` — без файла.
4. Значения по умолчанию.

Настройки проверяются при каждом запуске. Если что-то не так, программа печатает все проблемы
с именами переменных (без значений) и завершается с кодом 2, ничего не запуская: fail closed.
Неизвестные переменные `WEWLN_*` (обычно опечатки) не используются — `check-config`
выводит о них предупреждение.

## Переменные

### Общие и Notion

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `WEWLN_MODE` | `simulation` | `simulation` или `production`. Реальный звонок возможен только в `production` и только при всех условиях из [SECURITY.md](../SECURITY.md) |
| `WEWLN_PRODUCTION_CONFIRM` | пусто | для `production` обязательна точная фраза `I_ACCEPT_REAL_CALLS` |
| `WEWLN_KILL_SWITCH` | `false` | `true` блокирует любые звонки. То же во время работы: `wewln calls stop-all` (снять — `wewln calls resume`) |
| `WEWLN_TELEPHONY_PROVIDER` | `mock` | `mock` — симуляция; `asterisk` — свой Asterisk через ARI ([ASTERISK.md](ASTERISK.md)) |
| `WEWLN_HTTP_HOST` | `127.0.0.1` | IP-адрес или `localhost`. Для адреса не из loopback обязателен `WEWLN_API_TOKEN` |
| `WEWLN_HTTP_PORT` | `8787` | порт HTTP API (1–65535) |
| `WEWLN_API_TOKEN` | не задан | Bearer-токен для `/api/...`, не короче 32 символов. Без токена защищённые эндпоинты закрыты |
| `WEWLN_DATA_DIR` | `data` | папка данных (БД, логи) |
| `WEWLN_DB_PATH` | `<DATA_DIR>/wewln.db` | путь к SQLite |
| `WEWLN_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `WEWLN_LOG_FORMAT` | `json` | `json` (одна JSON-запись на строку) или `text` |
| `WEWLN_LOG_TO_FILE` | `true` | писать ли лог в файл (кроме консоли) |
| `WEWLN_LOG_FILE` | `<DATA_DIR>/logs/wewln.log` | файл лога; ротация 5 файлов по 5 МБ |
| `WEWLN_DEFAULT_LANGUAGE` | `ru-RU` | язык разговора; пока поддерживается только `ru-RU` |
| `WEWLN_TIMEZONE` | `UTC` | часовой пояс IANA, например `Asia/Baku` или `Europe/Moscow` |
| `WEWLN_DEFAULT_PHONE_REGION` | не задан | код страны ISO 3166-1 (`AZ`, `RU`, `KZ` …) для номеров без `+` и кода страны. Без него такие номера отклоняются («Номер без кода страны») |
| `WEWLN_NOTION_ENABLED` | `false` | включить синхронизацию с Notion в `serve`. Требует токен и все четыре ID баз |
| `WEWLN_NOTION_TOKEN` | не задан | токен подключения Notion (`ntn_…`). Лучше хранить его в Диспетчере учётных данных Windows (`wewln notion token set`), см. ниже |
| `WEWLN_NOTION_CAMPAIGNS_DB` | не задан | ID или ссылка на базу «Кампании (wewln)» |
| `WEWLN_NOTION_CONTACTS_DB` | не задан | ID или ссылка на базу «Контакты (wewln)» |
| `WEWLN_NOTION_JOBS_DB` | не задан | ID или ссылка на базу «Задания на звонки (wewln)» |
| `WEWLN_NOTION_RESULTS_DB` | не задан | ID или ссылка на базу «Результаты звонков (wewln)» |
| `WEWLN_NOTION_SYNC_INTERVAL` | `60` | секунд между циклами синхронизации в `serve` (15–86400) |
| `WEWLN_NOTION_MAX_RPS` | `2.5` | не больше стольких запросов к Notion в секунду (больше 0, до 100) |
| `WEWLN_NOTION_TIMEOUT` | `30` | секунд на один запрос к Notion (1–300) |
| `WEWLN_NOTION_API_BASE_URL` | `https://api.notion.com` | адрес API. Менять только для имитации (`wewln notion mock-server`): `http://` разрешён лишь для `127.0.0.1` и `localhost` |

### Звонки

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `WEWLN_CALL_REGIONS` | пусто | страны, в которые можно звонить: коды ISO через запятую (`AZ` или `AZ,RU`). Номера других стран не набираются. Для реальных звонков обязательна. Верный номер +7 или +994 не доказывает, что он доступен и что на него можно звонить |
| `WEWLN_CALL_RING_TIMEOUT` | `30` | секунд звонка без ответа, после которых wewln кладёт трубку: «нет ответа» (5–120) |
| `WEWLN_CALL_MAX_DURATION` | `300` | самый долгий разговор в секундах, затем wewln завершает звонок (10–3600) |
| `WEWLN_CALL_RETRY_DELAY` | `900` | секунд до следующей попытки после «занято» или «нет ответа» (60–86400). Число попыток — «Макс. попыток» кампании (1–5) |
| `WEWLN_MOCK_SCENARIO` | `answer` | поведение `mock`: `answer`, `busy`, `no_answer`, `reject`, `unreachable`, `fail`, `silent`, `answer_hold` или `mixed` (по последней цифре номера) |
| `WEWLN_CALLER_ID` | не задан | номер, который передаётся при звонке (E.164, например `+994121234567`). Только ваш номер у провайдера; провайдер может заменить его своим |

Номера платных и похожих служб (premium rate, shared cost) не набираются никогда.

### Asterisk

Нужны только при `WEWLN_TELEPHONY_PROVIDER=asterisk`. Настройка Asterisk — [ASTERISK.md](ASTERISK.md).

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `WEWLN_ARI_URL` | `http://127.0.0.1:8088` | адрес ARI: только схема, хост и порт (без `/ari`). `http://` разрешён лишь на этом компьютере (WSL2 пробрасывает `127.0.0.1`), для другого компьютера — `https://` |
| `WEWLN_ARI_USERNAME` | `wewln` | пользователь ARI из `ari.conf` |
| `WEWLN_ARI_PASSWORD` | не задан | пароль ARI. Лучше хранить в Диспетчере учётных данных Windows: `wewln telephony secret set ari-password` |
| `WEWLN_ARI_APP` | `wewln` | имя приложения Stasis; на этом Asterisk его не должно использовать ничто другое |
| `WEWLN_ASTERISK_ENDPOINT` | `PJSIP/{number}@wewln-trunk` | как набирать номер: `{number}` заменяется номером в формате `WEWLN_ASTERISK_NUMBER_FORMAT` |
| `WEWLN_ASTERISK_NUMBER_FORMAT` | `e164` | какой формат ждёт провайдер: `e164` (`+994501234567`), `digits` (`994501234567`) или `00` (`00994501234567`) |
| `WEWLN_ASTERISK_SOFTPHONE_ENDPOINT` | `PJSIP/softphone` | локальный софтфон (MicroSIP) для бесплатной проверки без телефонной сети: `wewln calls test --softphone`. Не может совпадать с SIP-транком |
| `WEWLN_ASTERISK_ANSWER_MEDIA` | `sound:hello-world` | что проиграть после ответа при `WEWLN_VOICE_MODE=off`. Пусто — сразу положить трубку |

### Голосовой движок

Устройство, установка и проверка — [VOICE.md](VOICE.md).

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `WEWLN_VOICE_MODE` | `off` | `off` — после ответа звучит `WEWLN_ASTERISK_ANSWER_MEDIA`; `ai` — голосовой разговор. Без моделей дозвонщик с `ai` не запускается |
| `WEWLN_VOICE_STT` | `vosk` | распознавание: `vosk`, `faster-whisper` или `whisper-http` (сервер whisper.cpp) |
| `WEWLN_VOICE_STT_MODEL` | пусто | Vosk — папка модели; faster-whisper — размер (`small`, `medium`, `large-v3`) или папка. Пусто: `<DATA_DIR>/models/vosk-model-small-ru-0.22` или `small` |
| `WEWLN_VOICE_STT_URL` | `http://127.0.0.1:8080` | адрес сервера whisper.cpp |
| `WEWLN_VOICE_STT_DEVICE` · `_COMPUTE_TYPE` | `cpu` · `int8` | только faster-whisper: `cpu`, `cuda`, `auto`; `int8` для CPU, `float16` для видеокарты |
| `WEWLN_VOICE_ALLOW_DOWNLOADS` | `false` | модели сами не скачиваются; `true` разрешает faster-whisper один раз скачать свою |
| `WEWLN_VOICE_LLM` | `ollama` | диалоговая модель: `ollama` или `openai` (локальный сервер с API OpenAI: llama.cpp, LM Studio) |
| `WEWLN_VOICE_LLM_URL` | `http://127.0.0.1:11434` | адрес сервера. `http://` — только этот компьютер и локальная сеть, иначе `https://` |
| `WEWLN_VOICE_LLM_MODEL` | `qwen3:4b` | модель (для Ollama — уже скачанная: `ollama pull qwen3:4b`) |
| `WEWLN_VOICE_LLM_API_KEY` | не задан | ключ, если сервер его требует; хранится как секрет |
| `WEWLN_VOICE_LLM_TEMPERATURE` · `_MAX_TOKENS` · `_TIMEOUT` | `0.5` · `160` · `15` | разнообразие ответов (0–1,5), предел длины ответа (32–1024), секунд до первых слов (2–120) |
| `WEWLN_VOICE_LLM_THINK` | `false` | режим размышлений моделей вроде Qwen3; выключенный отвечает намного быстрее |
| `WEWLN_VOICE_TTS` | `piper` | синтез: `piper` (файл голоса) или `piper-http` (`python -m piper.http_server`) |
| `WEWLN_VOICE_TTS_MODEL` | пусто | файл голоса `.onnx` (рядом — `.onnx.json`). Пусто: `<DATA_DIR>/models/ru_RU-denis-medium.onnx` |
| `WEWLN_VOICE_TTS_URL` · `_SPEAKER` · `_LENGTH_SCALE` | `http://127.0.0.1:5000` · не задан · `1.0` | сервер Piper, номер диктора многоголосых моделей, темп (1,2 — медленнее, 0,9 — быстрее) |
| `WEWLN_VOICE_GREETING` · `_GOODBYE` | см. `.env.example` | первая и последняя фраза разговора (2–400 символов) |
| `WEWLN_VOICE_TEST_SCRIPT` | см. `.env.example` | задача тестового звонка (10–4000 символов); звонки кампаний берут «Сценарий» кампании |
| `WEWLN_VOICE_MAX_TURNS` | `12` | реплик собеседника за звонок (1–40), затем прощание |
| `WEWLN_VOICE_NO_INPUT_TIMEOUT` | `8` | секунд тишины до «Алло? Вы меня слышите?» (3–60) |
| `WEWLN_VOICE_END_SILENCE_MS` | `700` | пауза, которая заканчивает реплику (300–3000 мс) |
| `WEWLN_VOICE_BARGE_IN` | `true` | собеседник может перебить wewln |
| `WEWLN_VOICE_AUDIOSOCKET_HOST` · `_PORT` | `127.0.0.1` · `9092` | где wewln принимает звук звонка от Asterisk. Asterisk в WSL2 с сетью NAT: адрес адаптера WSL в Windows ([ASTERISK.md](ASTERISK.md), шаг 6а). Порт не равен `WEWLN_HTTP_PORT` |
| `WEWLN_VOICE_AUDIOSOCKET_ADVERTISE` | пусто | адрес, к которому подключается Asterisk, если он не `HOST:PORT`; обязателен при `HOST=0.0.0.0` |
| `WEWLN_VOICE_STORE_TRANSCRIPTS` · `_TRANSCRIPT_DAYS` | `true` · `30` | хранить ли текст разговоров в локальной базе и сколько дней (1–3650) |

### Вебхуки статусов звонков

Необязательны: нужны только провайдеру, который сообщает о ходе звонка по HTTP.
Формат и защита — в [SECURITY.md](../SECURITY.md).

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `WEWLN_WEBHOOK_SECRET` | не задан | ключ подписи HMAC-SHA256, не короче 32 символов. Пока не задан, `POST /api/v1/telephony/events` закрыт. Создать и сохранить: `wewln telephony secret set webhook-secret --generate --show` |
| `WEWLN_WEBHOOK_TOLERANCE` | `300` | допустимая разница часов в секундах (30–900): более старые запросы отклоняются |

Относительные пути считаются от папки, из которой запущена программа.

ID баз печатает и при желании сам записывает в `.env` команда
`wewln notion bootstrap --parent <ссылка> --write-env`. Вместо ID подходит ссылка на базу
из Notion (из адреса берётся только ID). Подробно — в [NOTION.md](NOTION.md).

## Секреты

- `WEWLN_API_TOKEN`, `WEWLN_NOTION_TOKEN`, `WEWLN_ARI_PASSWORD`, `WEWLN_WEBHOOK_SECRET` и
  `WEWLN_VOICE_LLM_API_KEY` хранятся как `SecretStr`. В выводе `check-config`, в `/api/v1/info` и в логах вместо значения
  стоит только `set` или `not set`.
- Токен Notion ищется по порядку: переменная окружения `WEWLN_NOTION_TOKEN` → строка в `.env` →
  Диспетчер учётных данных Windows (запись `wewln-phonecaller`, команда
  `wewln notion token set`). `check-config` и `wewln notion token status` показывают источник:
  `set (environment variable)`, `set (.env file)` или `set (credential store)`.
- Пароль ARI и ключ вебхуков ищутся так же: окружение → `.env` → Диспетчер учётных данных
  (`wewln telephony secret set ari-password` и `wewln telephony secret set webhook-secret`).
  Источник показывает `wewln telephony secret status`; удалить — `wewln telephony secret delete`.
- Токен проверяется только по виду (без пробелов, разумная длина); неверный токен Notion
  отклонит при первом запросе, и `wewln notion check` об этом скажет.
- Пустое значение (`WEWLN_API_TOKEN=`) означает «не задан».
- Сгенерировать токен API:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `.env` исключён из git (`.gitignore`). Не храните настоящие токены в `.env.example`.

## Часовые пояса в Windows

В Windows нет системной базы часовых поясов IANA, поэтому её даёт пакет `tzdata` —
он ставится автоматически вместе с проектом.
