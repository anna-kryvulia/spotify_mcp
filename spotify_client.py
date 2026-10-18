"""
spotify_client.py — тонка обгортка над Spotify Web API (версія після змін лютого 2026).

Що робить:
  * читає токен із .spotify_token.json (його створює auth.py) і сам оновлює його,
    коли access token прострочився;
  * перетворює HTTP-помилки Spotify на SpotifyError з кодом, підказкою (hint)
    і прапорцем retryable — MCP-сервер віддає це агентам як зрозумілу помилку;
  * містить лише ті виклики, які потрібні проєкту.

Використовують: server.py (MCP-сервер), reset.py і evaluate.py (тестова обв'язка).
Агенти цей модуль НЕ імпортують — вони ходять лише через MCP-клієнт.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN_FILE = BASE_DIR / ".spotify_token.json"
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(Exception):
    """Помилка, яку можна показати агенту як є."""

    def __init__(self, code: str, message: str, hint: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = retryable

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message,
                "hint": self.hint, "retryable": self.retryable}


# ---------------------------------------------------------------- токени

def _load_token() -> dict:
    if not TOKEN_FILE.exists():
        raise SpotifyError("spotify_auth_error", "Немає .spotify_token.json.",
                           "Запусти `uv run auth.py` один раз, щоб авторизуватися.")
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def save_token(token: dict) -> None:
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600)) - 60
    TOKEN_FILE.write_text(json.dumps(token, indent=2), encoding="utf-8")


def _refresh(token: dict) -> dict:
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        raise SpotifyError("spotify_auth_error", "SPOTIFY_CLIENT_ID/SECRET не задані в .env.")
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = httpx.post(TOKEN_URL, timeout=20,
                   headers={"Authorization": f"Basic {basic}"},
                   data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
    if r.status_code != 200:
        raise SpotifyError("spotify_auth_error", f"Не вдалося оновити токен: {r.text[:200]}",
                           "Запусти `uv run auth.py` ще раз.")
    new = r.json()
    new.setdefault("refresh_token", token["refresh_token"])  # Spotify не завжди повертає новий
    save_token(new)
    return new


def _access_token(force_refresh: bool = False) -> str:
    token = _load_token()
    if force_refresh or time.time() >= token.get("expires_at", 0):
        token = _refresh(token)
    return token["access_token"]


# ---------------------------------------------------------------- HTTP

def request(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    """Один запит до Web API з обробкою 401 (одне оновлення токена) і мапінгом помилок."""
    url = path if path.startswith("http") else f"{API}{path}"
    for attempt in range(2):
        headers = {"Authorization": f"Bearer {_access_token(force_refresh=attempt == 1)}"}
        try:
            r = httpx.request(method, url, params=params, json=body, headers=headers, timeout=20)
        except httpx.HTTPError as e:
            raise SpotifyError("network_error", f"Spotify недоступний: {e}", "Спробуй ще раз.", True)

        if r.status_code == 401 and attempt == 0:
            continue  # прострочений токен — оновлюємо й повторюємо один раз
        if r.status_code in (200, 201):
            return r.json() if r.content else {}
        if r.status_code == 204:
            return {}
        _raise_for(r)
    raise SpotifyError("spotify_auth_error", "Токен недійсний після оновлення.", "Запусти `uv run auth.py`.")


def _raise_for(r: httpx.Response) -> None:
    try:
        detail = r.json().get("error", {})
        msg = detail.get("message", "") if isinstance(detail, dict) else str(detail)
        reason = detail.get("reason", "") if isinstance(detail, dict) else ""
    except ValueError:
        msg, reason = r.text[:200], ""

    if r.status_code == 429:
        wait = r.headers.get("Retry-After", "?")
        raise SpotifyError("rate_limited", f"Ліміт запитів Spotify ({reason or 'rate limit'}).",
                           f"Зачекай {wait} с і повтори.", True)
    if r.status_code == 403:
        raise SpotifyError("forbidden", f"Spotify заборонив дію: {msg}",
                           "Перевір скоупи токена, Premium власника застосунку і allowlist у Dashboard.")
    if r.status_code == 404:
        raise SpotifyError("not_found", f"Ресурс не знайдено: {msg}")
    if r.status_code == 400:
        raise SpotifyError("bad_request", f"Spotify відхилив запит: {msg}")
    raise SpotifyError("spotify_error", f"HTTP {r.status_code}: {msg}", "Спробуй ще раз.", r.status_code >= 500)


# ---------------------------------------------------------------- виклики API

def search_tracks(q: str, limit: int = 10, offset: int = 0) -> list[dict]:
    # Після лютого 2026: limit максимум 10, далі — пагінація через offset.
    data = request("GET", "/search", params={"q": q, "type": "track",
                                             "limit": min(limit, 10), "offset": offset})
    return data.get("tracks", {}).get("items", [])


def my_playlists() -> list[dict]:
    """Усі плейлисти поточного користувача (GET /me/playlists з пагінацією)."""
    out, url, params = [], "/me/playlists", {"limit": 50}
    while url:
        data = request("GET", url, params=params)
        out.extend(p for p in data.get("items", []) if p)
        url, params = data.get("next"), None  # next уже містить параметри
    return out


def me() -> dict:
    return request("GET", "/me")


def playlist_items(playlist_id: str) -> list[dict]:
    """Треки плейлиста. Поле треку тепер називається `item` (раніше `track`)."""
    out, url, params = [], f"/playlists/{playlist_id}/items", {"limit": 50}
    while url:
        data = request("GET", url, params=params)
        for row in data.get("items", []):
            t = row.get("item") or row.get("track")
            if t and t.get("uri", "").startswith("spotify:track:"):
                out.append(t)
        url, params = data.get("next"), None
    return out


def create_playlist(name: str, description: str = "") -> dict:
    # POST /users/{id}/playlists видалено — тепер лише POST /me/playlists.
    return request("POST", "/me/playlists",
                   body={"name": name, "description": description, "public": False})


def add_items(playlist_id: str, uris: list[str]) -> dict:
    # POST /playlists/{id}/tracks видалено — тепер /items.
    return request("POST", f"/playlists/{playlist_id}/items", body={"uris": uris})


def remove_from_library(uris: list[str]) -> dict:
    """Відписка від плейлиста (так Spotify «видаляє» власні плейлисти). Макс. 40 URI за раз."""
    return request("DELETE", "/me/library", params={"uris": ",".join(uris[:40])})


def simplify(track: dict) -> dict:
    """Компактне представлення треку для агентів (менше токенів)."""
    return {
        "uri": track.get("uri"),
        "name": track.get("name"),
        "artists": [a.get("name") for a in track.get("artists", [])],
        "album": (track.get("album") or {}).get("name"),
    }
