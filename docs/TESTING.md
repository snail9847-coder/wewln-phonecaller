# Тестирование

## Локальный запуск

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
```

Linux: те же команды через `.venv/bin/python`.

Тестам не нужны сеть, Notion, телефония и модели ИИ. Каждый тест работает во временной папке,
переменные `WEWLN_*` с компьютера в тесты не попадают.

## Что проверяют тесты

| Файл | Что проверяется |
| --- | --- |
| `tests/test_config.py` | значения по умолчанию, `.env` в UTF-8, приоритет окружения, отказ при опасных настройках, отсутствие секретов в сообщениях об ошибках |
| `tests/test_safety.py` | гейт реальных звонков: каждая причина блокировки по отдельности и все вместе |
| `tests/test_redact.py` | маскировка номеров в разных форматах, защита дат и времени от маскировки, маскировка токенов |
| `tests/test_logging.py` | JSON- и текстовые логи, `request_id` и `call_id`, маскировка в сообщениях, исключениях и полях, файл в UTF-8 |
| `tests/test_db.py` | миграции и их повторный запуск, WAL, откат неудачной миграции, проверка без создания файла, маскировка номеров на диске |
| `tests/test_api.py` | `/health`, `/ready` (200 и 503), `request_id`, токен API, JSON-ошибки 401, 404, 405, 409 и 500 без внутренних деталей |
| `tests/test_cli.py` | `check-config`, `init-db`, `serve` с ошибочными настройками, `--env-file`, `--version`, коды выхода, отсутствие секретов в выводе |
| `tests/test_env_example.py` | `.env.example` корректен и описывает все настройки |
| `tests/test_smoke.py` | настоящий процесс `serve`: ответы `/health`, `/ready`, `/api/v1/info`, файл лога, аудит запуска и остановки |

## CI

`.github/workflows/ci.yml` запускается при push в `main`, на каждый pull request и вручную.
Матрица: `ubuntu-latest` и `windows-latest` × Python 3.12, 3.13 и 3.14. Шаги: установка,
`ruff check`, `ruff format --check`, `pytest`, `check-config`.

## Что тесты не покрывают

- Ручную проверку на ПК владельца с Windows 10/11. CI на `windows-latest` — это Windows Server,
  а не домашняя Windows 10/11. Чек-лист ручной проверки появится в фазе 7.
- Остановку по `Ctrl+C` в Windows. В CI процесс на Windows завершается принудительно
  (`TerminateProcess`), поэтому штатная остановка и запись `app.stop` проверяются только на Linux.
- Реальные звонки, Notion и модели ИИ — их ещё нет. В будущем они проверяются на моках;
  реальные проверки — только с разрешения владельца.
