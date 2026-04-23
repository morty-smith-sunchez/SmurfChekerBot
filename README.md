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
cd "c:\Users\Дима\Documents\my projects\dota_profile_bot"
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

### Команды
- `/start` — помощь
- `/analyze <steamid64 | account_id | ссылка>` — анализ профиля

Примеры:
- `/analyze 76561198xxxxxxxxx`
- `/analyze 123456789`
- `/analyze https://www.dotabuff.com/players/123456789`

