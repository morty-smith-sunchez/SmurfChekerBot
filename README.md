## Dota 2 Profile Analyzer Telegram Bot

Бот анализирует профиль игрока Dota 2 по публичным данным и выдаёт:
- активность (матчи/день) за 30/90 дней
- винрейт за 30/90 дней и общий (если доступно)
- топ героев и “смену пулла” (30 vs 90 дней)
- подозрительность на **смурф** или **купленный** аккаунт (эвристический скоринг)

### Источники данных
- **OpenDota API** (без ключа работает, но лимиты ниже)
- **Steam Web API** (опционально) — Steam Level и базовая инфа профиля
- **STRATZ API** (опционально) — резерв по total матчам/WR
- **Dotabuff** (публичная страница игрока) — резерв по total матчам/WR

### Быстрый старт (Windows / PowerShell)

1) Установка зависимостей:

```bash
cd /path/to/dota_profile_bot
python -m pip install -r requirements.txt
```

2) Создайте `.env` рядом с `bot.py`:

```env
TELEGRAM_BOT_TOKEN=...
# опционально:
STEAM_API_KEY=...
OPENDOTA_API_KEY=...
STRATZ_API_KEY=...
HTTP_PROXY=http://127.0.0.1:8080
HTTPS_PROXY=http://127.0.0.1:8080
# блок пожертвований (опционально):
DONATION_TEXT=Поддержать проект: карта 0000 0000 0000 0000
DONATION_URL=https://example.com/donate
```

Для GitHub:
- коммитьте только `.env.example`
- файл `.env` с реальными ключами уже исключен через `.gitignore`
- перед публикацией проверьте, что секреты не попали в историю коммитов

3) Запуск:

```bash
python bot.py
```

### MTProto версия (для proxy server + secret)

Если обычный `bot.py` не может достучаться до `api.telegram.org`, используйте `bot_mtproto.py`.

Нужно добавить в `.env`:

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_MTPROXY_SERVER=127.0.0.1
TELEGRAM_MTPROXY_PORT=1443
TELEGRAM_MTPROXY_SECRET=...
```

`TELEGRAM_API_ID` и `TELEGRAM_API_HASH` берутся на [my.telegram.org](https://my.telegram.org).

Запуск MTProto-версии:

```bash
python bot_mtproto.py
```

### FastAPI (REST API)

Проект также можно поднять как REST API (для сайтов, интеграций, автоматизации):

```bash
python -m pip install -r requirements.txt
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

Эндпоинты (Swagger-документация: http://127.0.0.1:8000/docs):

| Метод | Путь | Описание |
|---|---|---|
| GET | `/health` | проверка живости сервиса |
| POST | `/api/v1/analyze` | анализ профиля, JSON `{"player": "steamid64/account_id/ссылка"}` |
| POST | `/api/v1/analyze/{player}` | то же, но id/ссылка в пути |
| GET | `/api/v1/match/{match_id}` | сводка по матчу |
| GET | `/api/v1/match?match=...` | сводка по матчу через query-параметр |

Ответ `/api/v1/analyze` содержит `account_id`, `steamid64`, `report_html` (уже HTML с тегами Telegram) и `report_png_base64` (список PNG-карточек в base64).

Пример через PowerShell/curl:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/analyze `
  -ContentType 'application/json' `
  -Body '{"player": "123456789"}'
```

### Структура проекта

```
dota_profile_bot/
├── bot.py                 # Telegram-бот (aiogram): команды, отчёты, клавиатуры
├── bot_mtproto.py         # Альтернативная версия бота (Telethon + MTProxy)
├── api.py                 # FastAPI REST-обёртка над анализом (uvicorn)
├── config.py              # Настройки из .env через pydantic-settings
├── smoke_test.py          # Быстрая проверка доступности внешних API
├── requirements.txt       # Зависимости
├── .env.example           # Шаблон переменных окружения
├── analysis/              # Логика анализа профиля
│   ├── metrics.py         #   статистика за 30/90 дней (матчи, WR, герои, роли)
│   ├── scoring.py         #   эвристики: смурф / буст / купленный аккаунт
│   └── learning.py        #   адаптивные пороги на подтверждённых кейсах
├── analytics/             # Локальная SQLite-аналитика (store.py)
├── dota/                  # Клиенты внешних API
│   ├── opendota_client.py #   OpenDota
│   ├── steam_client.py    #   Steam Web API
│   ├── stratz_client.py   #   STRATZ
│   └── dotabuff_client.py #   Dotabuff (парсер публичной страницы)
├── rendering/             # Рендер PNG-карточек отчёта (Pillow/pilmoji)
├── utils/                 # Вспомогательные парсеры ID и ссылок (parse_ids.py)
├── assets/                # Фоны карточек (welcome_banner, report_background) и шрифты
├── data/                  # Локальные данные (SQLite/JSON) — не попадает в git
└── tests/                 # Pytest-тесты (test_api.py)
```

### Команды
- `/start` — помощь
- `/analyze <steamid64 | account_id | ссылка>` — анализ профиля

Примеры:
- `/analyze 76561198xxxxxxxxx`
- `/analyze 123456789`
- `/analyze https://www.dotabuff.com/players/123456789`

