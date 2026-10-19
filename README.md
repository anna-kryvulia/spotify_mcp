# Spotify Playlist Manager — 2 агенти + власний MCP-сервер

Мініпроєкт з Generative AI: користувач пише запит природною мовою, система знаходить треки
й створює або доповнює плейлист у Spotify.

## Архітектура

```
Користувач
   │
   ▼
[A] Планувальник ── MCP-клієнт ──► server.py (AGENT_ROLE=planner)
   │  інструменти: search_tracks, get_playlist        (тільки читання)
   │  результат: JSON-план
   ▼
[B] Виконавець/Перевіряльник ── MCP-клієнт ──► server.py (AGENT_ROLE=executor)
   │  інструменти: get_playlist, save_playlist        (єдиний, хто пише)
   │  план поганий → reject → A переплановує (1 раз)
   ▼
Spotify Web API
         все логується → traces/traces.jsonl + кольорова консоль
```

Право на запис захищене двічі: клієнт планувальника не бачить `save_playlist`,
а сервер, запущений з `AGENT_ROLE=planner`, відмовляє в записі навіть при прямому виклику.

## Файли

| Файл | Призначення |
|---|---|
| `server.py` | MCP-сервер: `search_tracks`, `get_playlist`, `save_playlist` (запис). Валідація + структуровані помилки |
| `spotify_client.py` | Обгортка Spotify Web API (ендпоінти після змін лютого 2026), оновлення токена, мапінг помилок |
| `agents.py` | MCP-клієнт, цикл агента, Планувальник + Виконавець, базовий single-agent |
| `tracing.py` | Трейсинг у JSONL і консоль |
| `auth.py` | Одноразова OAuth-авторизація Spotify |
| `evaluate.py` | 5 тестів, автоматична перевірка стану Spotify, порівняння multi vs single |
| `reset.py` | Прибирає тестові плейлисти перед прогоном |
| `check_openrouter.py` | Перевіряє ключ, ліміти й те, що модель вміє tool calling |
| `pyproject.toml`, `uv.lock`, `.python-version` | Залежності проєкту для uv. `uv.lock` фіксує точні версії — комітьте його, тоді вся команда має однакове середовище |

## Запуск

Потрібен [uv](https://docs.astral.sh/uv/getting-started/installation/). Python окремо ставити не треба:
uv сам завантажить версію з `.python-version`.

1. **Spotify.** Власник застосунку має мати **Premium**. На https://developer.spotify.com/dashboard
   створи застосунок (API: Web API), Redirect URI: `http://127.0.0.1:8888/callback`.
2. **OpenRouter.** Створи ключ на https://openrouter.ai/settings/keys.
3. Встанови залежності й налаштуй середовище:
   ```bash
   uv sync                          # створює .venv і ставить версії з uv.lock
   cp .env.example .env             # заповни ключі (Windows: copy .env.example .env)
   uv run check_openrouter.py       # модель має відповісти tool call-ом
   uv run auth.py                   # відкриється браузер, увійди в Premium-акаунт
   ```
4. Один запит:
   ```bash
   uv run agents.py "Створи плейлист з 5 пісень Radiohead без live-версій, назви його Radiohead Essentials"
   uv run agents.py "те саме" --mode single
   ```
5. Оцінка: `uv run evaluate.py` (або `--mode multi`, `--mode single`).

Ручна перевірка сервера без LLM: `uv run mcp dev server.py` відкриває MCP Inspector.

Додати залежність: `uv add <пакет>` (оновить `pyproject.toml` і `uv.lock`).

## Бюджет запитів до LLM

Безкоштовні моделі OpenRouter: 20 запитів/хв і **50 запитів/день** на акаунт, поки на ньому
не куплено кредитів на $10 (тоді 1000/день). Невдалі запити теж рахуються.

Орієнтовно: multi — 6–8 викликів LLM на запит, single — 4–6. Повний `evaluate.py`
(5 тестів × 2 режими) — **приблизно 50–70 викликів**, тобто більше денного ліміту.
Варіанти: запускати `--mode multi` і `--mode single` у різні дні або з ключів різних
учасників команди, або один раз поповнити рахунок на $10.

Для чесного порівняння не використовуйте `openrouter/free`: цей роутер щоразу обирає
випадкову модель. Зафіксуйте одну модель в `OPENROUTER_MODEL`.

## Тестові запити

| # | Запит | Перевірка |
|---|---|---|
| 1 | 5 пісень Radiohead без live, «Radiohead Essentials» | 5 треків Radiohead, без live |
| 2 | Той самий запит ще раз | `playlist_exists` → створено «Radiohead Essentials (2)», оригінал не змінився |
| 3 | «French Touch»: 3 Daft Punk + 3 Justice без реміксів | 3/3/6, без remix |
| 4 | Додай 2 пісні Massive Attack у French Touch | 8 треків, 2 Massive Attack, без нового плейлиста |
| 5 | 10 пісень гурту Xqzzyt Brothers (не існує) | нічого не створено |

Тест 2 — сценарій «помилка й вихід із неї» для відео-демо.
