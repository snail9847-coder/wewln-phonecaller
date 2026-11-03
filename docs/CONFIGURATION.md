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

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `WEWLN_MODE` | `simulation` | `simulation` или `production`. В этой версии реальных звонков нет ни в одном режиме |
| `WEWLN_PRODUCTION_CONFIRM` | пусто | для `production` обязательна точная фраза `I_ACCEPT_REAL_CALLS` |
| `WEWLN_KILL_SWITCH` | `false` | `true` блокирует любые реальные звонки |
| `WEWLN_TELEPHONY_PROVIDER` | `mock` | провайдер телефонии; пока есть только `mock` (симуляция) |
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
| `WEWLN_NOTION_ENABLED` | `false` | включить интеграцию с Notion (появится в фазе 3) |
| `WEWLN_NOTION_TOKEN` | не задан | токен внутренней интеграции Notion; обязателен при `WEWLN_NOTION_ENABLED=true` |

Относительные пути считаются от папки, из которой запущена программа.

## Секреты

- `WEWLN_API_TOKEN` и `WEWLN_NOTION_TOKEN` хранятся как `SecretStr`. В выводе `check-config`,
  в `/api/v1/info` и в логах вместо значения стоит только `set` или `not set`.
- Пустое значение (`WEWLN_API_TOKEN=`) означает «не задан».
- Сгенерировать токен API:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `.env` исключён из git (`.gitignore`). Не храните настоящие токены в `.env.example`.

## Часовые пояса в Windows

В Windows нет системной базы часовых поясов IANA, поэтому её даёт пакет `tzdata` —
он ставится автоматически вместе с проектом.
