# Asterisk: установка в WSL2 и подключение SIP-провайдера

wewln не работает с SIP напрямую. Звонки совершает **Asterisk** — бесплатная АТС с открытым
кодом. На Windows он работает внутри WSL2 (Linux в Windows). wewln управляет им через
**ARI** (Asterisk REST Interface): HTTP-запросы и WebSocket с событиями на `127.0.0.1:8088`.

```text
wewln (Windows) ──ARI: HTTP + WebSocket, 127.0.0.1:8088──▶ Asterisk (WSL2, Ubuntu)
                                                            ├─ PJSIP/softphone ──▶ MicroSIP на этом же ПК
                                                            │                      (без телефонной сети, $0)
                                                            └─ PJSIP/wewln-trunk ──SIP──▶ провайдер ──▶ телефоны
```

До телефонов звонок доходит **только** через SIP-транк провайдера (`wewln-trunk`). Без
аккаунта у провайдера можно проверить всё, кроме этого последнего участка: управление
Asterisk, статусы звонков, аварийную остановку и звонок на софтфон на своём ПК.

## Что проверено, а что нет

| Что | Как проверено |
| --- | --- |
| Протокол ARI: создание звонка, события, завершение, ошибки, переподключение | автотесты с имитацией ARI (`tests/ari_simulator.py`, `tests/test_asterisk.py`) |
| Настоящий Asterisk из пакетов Ubuntu: 20 LTS (Ubuntu 24.04) и 22 LTS (Ubuntu 26.04) | CI, задание `live Asterisk (ARI)`: звонки на Local-каналы тестового диалплана — ответ, занято, отклонён, перегрузка сети, нет ответа, аварийная остановка, повтор запроса с тем же ID. Там же проверяется, что примеры конфигураций из `deploy/asterisk` загружаются |
| SIP-транк, звонки в телефонную сеть +7 и +994 | **не проверено**: нужен аккаунт у провайдера и отдельное разрешение владельца |
| Софтфон MicroSIP на Windows с Asterisk в WSL2 | **[не проверено]**: инструкция ниже составлена по документации, на Windows не запускалась |

## Требования

- Windows 10 версии 2004 (сборка 19041) и новее или Windows 11 — для WSL2
  ([Microsoft](https://learn.microsoft.com/en-us/windows/wsl/install)).
- Включённая виртуализация в BIOS/UEFI, 64-битный процессор.
- Около 1 ГБ места для Ubuntu и Asterisk.

Версии Asterisk ([docs.asterisk.org](https://docs.asterisk.org/About-the-Project/Asterisk-Versions/)):

| Ubuntu в WSL | Asterisk из пакета | Поддержка ветки |
| --- | --- | --- |
| **26.04 LTS (рекомендуется)** | 22.5.2 | 22 LTS: исправления до 16.10.2028, исправления безопасности до 16.10.2029 |
| 24.04 LTS | 20.6.0 | 20 LTS: с 19.10.2026 только исправления безопасности, конец поддержки 19.10.2027 |

## Шаг 1. WSL2 и Ubuntu

В PowerShell **от имени администратора**:

```powershell
wsl --install -d Ubuntu-26.04
```

После перезагрузки откроется окно Ubuntu: придумайте имя пользователя и пароль Linux.
Список доступных версий: `wsl --list --online`. В Ubuntu, установленной через
`wsl --install`, systemd включён по умолчанию
([Microsoft](https://learn.microsoft.com/en-us/windows/wsl/systemd)); проверка:
`systemctl is-system-running` (ответ `running` или `degraded` — нормально).

WSL выключает Ubuntu, когда в ней ничего не запущено (через 15 секунд после закрытия
последнего окна), а вместе с ней и Asterisk. Пока wewln звонит, держите окно Ubuntu открытым
или отключите автоостановку в файле `%UserProfile%\.wslconfig`
([Microsoft](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)):

```ini
[general]
instanceIdleTimeout=-1
```

## Шаг 2. Установка Asterisk

В окне Ubuntu:

```bash
sudo apt update
sudo apt install -y asterisk asterisk-core-sounds-en-gsm
asterisk -V
```

## Шаг 3. Настройка

Готовые файлы лежат в папке [`deploy/asterisk`](../deploy/asterisk) репозитория. Диск `C:`
виден в Ubuntu как `/mnt/c`. Замените путь на свою папку с wewln:

```bash
cd /mnt/c/Users/ИМЯ/wewln-phonecaller/deploy/asterisk
sudo cp -a /etc/asterisk /etc/asterisk.orig     # копия исходных настроек
sudo cp http.conf ari.conf modules.conf pjsip.conf extensions.conf /etc/asterisk/
```

| Файл | Что в нём |
| --- | --- |
| `http.conf` | веб-сервер Asterisk только на `127.0.0.1:8088` |
| `ari.conf` | пользователь ARI `wewln` |
| `modules.conf` | не загружать устаревший `chan_sip` (в Asterisk 20 он ещё есть и мешает PJSIP) |
| `pjsip.conf` | софтфон `softphone` и SIP-транк `wewln-trunk` (заполняется в шаге 7) |
| `extensions.conf` | входящие с транка отклоняются; софтфону доступен только эхо-тест 600 |

Придумайте два длинных случайных пароля:

```bash
openssl rand -hex 24   # для ARI
openssl rand -hex 16   # для софтфона
```

Впишите их вместо `CHANGE_ME_ARI_PASSWORD` в `/etc/asterisk/ari.conf` и вместо
`CHANGE_ME_SOFTPHONE_PASSWORD` в `/etc/asterisk/pjsip.conf` (`sudo nano <файл>`; сохранить —
Ctrl+O, Enter, выйти — Ctrl+X). Закройте файлы с паролями от других пользователей и
перезапустите Asterisk:

```bash
sudo chown root:asterisk /etc/asterisk/ari.conf /etc/asterisk/pjsip.conf
sudo chmod 640 /etc/asterisk/ari.conf /etc/asterisk/pjsip.conf
sudo systemctl enable asterisk
sudo systemctl restart asterisk
```

## Шаг 4. Проверка Asterisk

```bash
sudo asterisk -rx "core show version"
sudo asterisk -rx "pjsip show endpoints"        # softphone и wewln-trunk
curl -u wewln http://127.0.0.1:8088/ari/asterisk/info   # curl спросит пароль ARI
```

Ответ `curl` в формате JSON с версией Asterisk означает, что ARI работает.

## Шаг 5. Подключение wewln

В PowerShell в папке wewln (с активированным `.venv`). Пароль ARI сохраняется в Диспетчере
учётных данных Windows, ввод скрыт:

```powershell
wewln telephony secret set ari-password
```

В `.env`:

```dotenv
WEWLN_TELEPHONY_PROVIDER=asterisk
WEWLN_ARI_URL=http://127.0.0.1:8088
WEWLN_ARI_USERNAME=wewln
```

Проверка:

```powershell
wewln check-config
wewln telephony check
```

`telephony check` показывает версию Asterisk, состояние транка и почему реальные звонки пока
заблокированы. WSL2 пересылает `127.0.0.1` из Windows в Linux (`localhostForwarding`,
включено по умолчанию), поэтому адрес `127.0.0.1:8088` работает и из Windows. Режим остаётся
`simulation`: кампании и звонки на номера через Asterisk заблокированы.

## Шаг 6. Бесплатная проверка с софтфоном MicroSIP

**[не проверено]** Звонок на софтфон на этом же компьютере не выходит в телефонную сеть и
ничего не стоит, поэтому он разрешён и в режиме `simulation`.

1. Скачайте MicroSIP — бесплатный софтфон для Windows с открытым кодом
   ([microsip.org](https://www.microsip.org/)).
2. Узнайте адрес Ubuntu в PowerShell: `wsl hostname -I` (первый адрес, например
   `172.28.144.5`). Порт SIP по UDP из Windows доступен по этому адресу, а не по `127.0.0.1`.
   Адрес меняется после перезапуска WSL.
3. В MicroSIP: Menu → Add Account:
   - SIP Server и Domain — адрес из пункта 2;
   - Username и Login — `softphone`;
   - Password — пароль софтфона из шага 3;
   - Transport — UDP.
4. Наберите `600`: вы услышите приглашение и своё эхо — софтфон и звук работают.
5. В PowerShell: `wewln calls test --softphone`. MicroSIP зазвонит. Ответьте: прозвучит
   «Hello world», wewln положит трубку и напечатает `completed`.

Если MicroSIP не регистрируется или не звонит либо звонок есть, а звука нет, разрешите MicroSIP
в брандмауэре Windows для частных **и** общественных сетей: сеть WSL2 Windows обычно считает
общественной. Windows спрашивает об этом при первом запуске MicroSIP, позже — «Брандмауэр
Защитника Windows» → «Разрешение взаимодействия с приложением или компонентом».

## Шаг 7. Подключение SIP-провайдера

Нужен аккаунт у SIP-провайдера с исходящими звонками. Обычно это **платно**; сравнение
провайдеров — в [PROVIDERS.md](PROVIDERS.md). Ничего не покупайте и не пополняйте ради
проверки wewln: шаги 1–6 работают без провайдера.

Из личного кабинета провайдера понадобятся SIP-сервер, логин, пароль, формат номера и номер,
который можно показывать как Caller ID. В `/etc/asterisk/pjsip.conf` замените
`CHANGE_ME_SIP_SERVER`, `CHANGE_ME_SIP_LOGIN` и `CHANGE_ME_SIP_PASSWORD`. Если провайдер
требует регистрацию, раскомментируйте блок `[wewln-trunk-reg]`. Затем:

```bash
sudo systemctl restart asterisk
sudo asterisk -rx "pjsip show endpoint wewln-trunk"
sudo asterisk -rx "pjsip show contacts"         # Avail — сервер провайдера отвечает
sudo asterisk -rx "pjsip show registrations"    # Registered — если включена регистрация
```

Настройки wewln:

| Переменная | Что указать |
| --- | --- |
| `WEWLN_ASTERISK_ENDPOINT` | `PJSIP/{number}@wewln-trunk` (как в примере) |
| `WEWLN_ASTERISK_NUMBER_FORMAT` | как провайдер ждёт номер: `e164` (`+994501234567`), `digits` (`994501234567`) или `00` (`00994501234567`) — уточните у провайдера |
| `WEWLN_CALLER_ID` | ваш номер у провайдера; провайдер может заменить его своим. Чужой номер ставить нельзя |

`wewln telephony check` покажет состояние транка (`online`, `offline` или `unknown`).

## Шаг 8. Первый реальный звонок — только с отдельного разрешения

Реальный звонок требует сразу всего: режим `production` с фразой подтверждения, настроенный
Asterisk, список стран `WEWLN_CALL_REGIONS`, выключенную аварийную остановку и разрешение
оператора на **один** звонок на **один** номер:

```dotenv
WEWLN_MODE=production
WEWLN_PRODUCTION_CONFIRM=I_ACCEPT_REAL_CALLS
WEWLN_CALL_REGIONS=AZ
```

```powershell
wewln calls permit --to +994XXXXXXXXX    # подтверждение: последние 4 цифры номера
wewln calls test --to +994XXXXXXXXX
```

Разрешение действует 15 минут (`--minutes`, до 60) и на один звонок. Звоните только на свой
номер или людям, которые на это согласились. После ответа звучит английская фраза
«Hello world» из комплекта Asterisk: голосового движка ещё нет (фаза 5). Пустое
`WEWLN_ASTERISK_ANSWER_MEDIA=` — положить трубку сразу после ответа.

Звонки по кампаниям через Asterisk в этой версии **заблокированы** (`wewln calls run`
отказывает): включение кампаний, расписание и лимиты — фаза 6.

Аварийная остановка: `wewln calls stop-all` (или `POST /api/v1/calls/stop-all`) завершает все
звонки и запрещает новые до `wewln calls resume`. На стороне Asterisk:
`sudo asterisk -rx "channel request hangup all"`.

## Безопасность

- ARI слушает только `127.0.0.1`. wewln отказывается от `http://` к другим адресам: для
  Asterisk на другом компьютере нужен `https://`.
- Пароль ARI хранится в Диспетчере учётных данных Windows, передаётся только в заголовке
  `Authorization` и не попадает в URL и логи.
- В диалплане нет выхода в телефонную сеть: исходящие звонки создаёт только wewln через ARI.
  Входящие с транка отклоняются, софтфону доступен только эхо-тест. Украденный пароль
  софтфона не даёт звонить за ваш счёт.
- Каналы, которые попали в приложение `wewln`, но не были созданы wewln, сразу завершаются.
  Если то же имя приложения займёт другая программа, wewln перестаёт звонить
  (`ari_app_replaced`).
- В режиме NAT (по умолчанию в WSL2) порт SIP 5060 доступен только с этого ПК. Не
  пробрасывайте 5060 и 8088 на роутере. В режиме `networkingMode=mirrored` порты Ubuntu
  становятся портами Windows: закройте их брандмауэром.
- У провайдера: предоплата с небольшим балансом, лимит расходов, отключённые ненужные
  направления — так ошибка или взлом не обойдутся дорого.

## Если что-то не работает

| Сообщение | Причина | Что сделать |
| --- | --- | --- |
| `cannot connect to Asterisk ARI` | Ubuntu или Asterisk не запущены | откройте Ubuntu (`wsl -d Ubuntu-26.04`), `sudo systemctl status asterisk`; см. автоостановку WSL в шаге 1 |
| `ARI rejected the user name or password` | пароли в `ari.conf` и в wewln разные | `wewln telephony secret set ari-password` |
| `ARI answered HTTP 404` | ARI выключен | `enabled = yes` в `ari.conf` и `http.conf`; `sudo asterisk -rx "module show like res_ari"` |
| `the trunk endpoint does not exist` | имя транка не совпадает | `pjsip show endpoints`; имя в `WEWLN_ASTERISK_ENDPOINT` |
| `Asterisk refused the call request (HTTP 400)` | неверная строка набора | проверьте `WEWLN_ASTERISK_ENDPOINT` |
| `Asterisk ARI error (HTTP 500)` | Asterisk не смог создать канал: например, нет такого endpoint | `pjsip show endpoints`, `WEWLN_ASTERISK_SOFTPHONE_ENDPOINT` |
| `Asterisk ARI did not answer to the call request` или причина `provider_uncertain` | ответ на запрос набора не пришёл (тайм-аут, обрыв связи, Ctrl+C), и неизвестно, начался ли звонок | wewln этот номер в этой попытке не повторяет и кладёт трубку, если звонок найдётся; проверьте `sudo asterisk -rx "core show channels"` и `wewln calls list` |
| `another program took over the ARI application` | второй wewln или другая программа с тем же `WEWLN_ARI_APP` | остановите её и запустите wewln снова |
| звонок `failed`, причина `provider_refused` | провайдер ответил SIP 401/402/403/407: логин, баланс или запрет направления | проверьте настройки и баланс у провайдера |
| причина `state_lost` или `hangup_unconfirmed` | wewln перезапустился во время звонка или Asterisk не подтвердил завершение | `sudo asterisk -rx "core show channels"`; повисший канал: `channel request hangup <имя>` |

## Как wewln понимает, чем закончился звонок

По коду причины Q.850 от Asterisk и коду ответа SIP
([таблица соответствий](https://docs.asterisk.org/Configuration/Miscellaneous/Hangup-Cause-Mappings/)):

| Код | Статус wewln | Повтор |
| --- | --- | --- |
| ответ, затем любое завершение | `completed` — разговор состоялся | нет |
| 17 или SIP 486/600 — занято | `busy` | да, через `WEWLN_CALL_RETRY_DELAY` |
| 18, 19 или SIP 408/480; никто не ответил за `WEWLN_CALL_RING_TIMEOUT` | `no_answer` | да |
| 21 или SIP 603 — отклонён | `rejected` | нет |
| 1, 22, 27, 28 или SIP 404/410/484 — номер не существует или недоступен | `unreachable` | нет |
| SIP 401/402/403/407 — провайдер отказал | `failed` (`provider_refused`) | нет |
| 34 и другие сбои сети | `failed` | да |

Повторов не больше, чем «Макс. попыток» кампании. Запрос, на который Asterisk не ответил,
никогда не повторяется автоматически: звонок мог начаться.
