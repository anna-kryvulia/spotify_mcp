"""
  search_tracks   (читання)  пошук треків артиста з фільтром слів (live, remix…)
  get_playlist    (читання)  знайти плейлист користувача і показати його треки
  save_playlist   (ЗАПИС)    створити плейлист або дописати треки в існуючий
"""

from __future__ import annotations

import os
import re

from mcp.server.fastmcp import FastMCP

import spotify_client as sp
from spotify_client import SpotifyError

ROLE = os.environ.get("AGENT_ROLE", "executor")  # planner | executor | single
URI_RE = re.compile(r"^spotify:track:[A-Za-z0-9]{22}$")
MAX_TRACKS = 50

mcp = FastMCP("spotify", log_level="WARNING")


def err(code: str, message: str, hint: str = "", retryable: bool = False) -> dict:
    return {"error": code, "message": message, "hint": hint, "retryable": retryable}


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word.lower())}\b", (text or "").lower()) is not None


def _find_playlist(name_or_id: str) -> dict | None:
    key = name_or_id.strip().lower()
    for p in sp.my_playlists():
        if p.get("id") == name_or_id.strip() or (p.get("name") or "").strip().lower() == key:
            return p
    return None


# ------------------------------------------------------------------ tools

@mcp.tool()
def search_tracks(artist: str, query: str = "", limit: int = 10,
                  exclude: list[str] | None = None) -> dict:
    """Search Spotify tracks by one artist.

    Args:
        artist: exact artist name, e.g. "Radiohead". One artist per call.
        query: optional extra keywords (song title, album). Leave empty for general search.
        limit: how many tracks to return after filtering (1-30).
        exclude: words that must NOT appear in the track or album title,
                 e.g. ["live", "remix", "remaster"]. Whole-word, case-insensitive.

    Returns {"tracks": [{uri, name, artists, album}], "count", "filtered_out"}
    or an error object. Only tracks where `artist` is among the credited artists are kept;
    duplicate song titles are removed.
    """
    try:
        artist = (artist or "").strip()
        if not artist:
            return err("invalid_input", "Параметр artist порожній.", "Передай ім'я одного артиста.")
        if not isinstance(limit, int) or not 1 <= limit <= 30:
            return err("invalid_input", f"limit={limit} поза межами 1–30.", "Використай limit від 1 до 30.")
        exclude = [w.strip() for w in (exclude or []) if w and w.strip()]

        q = f'artist:"{artist}" {query}'.strip()
        found, seen_titles, filtered = [], set(), 0
        for page in range(6):
            batch = sp.search_tracks(q, limit=10, offset=page * 10)
            if not batch:
                break
            for t in batch:
                artists = [a.get("name", "").lower() for a in t.get("artists", [])]
                title = (t.get("name") or "")
                album = (t.get("album") or {}).get("name", "")
                if artist.lower() not in artists:
                    filtered += 1
                    continue
                if any(_has_word(title, w) or _has_word(album, w) for w in exclude):
                    filtered += 1
                    continue
                norm = re.sub(r"\s*[-(\[].*$", "", title).strip().lower()
                if norm in seen_titles:
                    filtered += 1
                    continue
                seen_titles.add(norm)
                found.append(sp.simplify(t))
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break

        if not found:
            return err("no_tracks_found",
                       f"Не знайдено треків артиста '{artist}' (відфільтровано {filtered}).",
                       "Перевір написання імені або зменш список exclude. Якщо артиста не існує — "
                       "повідом користувача і НЕ створюй плейлист.")
        return {"tracks": found, "count": len(found), "filtered_out": filtered}
    except SpotifyError as e:
        return e.to_dict()
    except Exception as e:
        return err("internal_error", f"{type(e).__name__}: {e}")


@mcp.tool()
def get_playlist(name_or_id: str) -> dict:
    """Find one of the user's own playlists by exact name (case-insensitive) or id
    and return its tracks.

    Returns {"id", "name", "total", "tracks": [{uri, name, artists, album}]}
    or {"error": "playlist_not_found", ...} with existing playlist names in the hint.
    """
    try:
        if not (name_or_id or "").strip():
            return err("invalid_input", "name_or_id порожній.", "Передай назву плейлиста.")
        p = _find_playlist(name_or_id)
        if p is None:
            names = [x.get("name") for x in sp.my_playlists()][:15]
            return err("playlist_not_found", f"Плейлист '{name_or_id}' не знайдено.",
                       f"Наявні плейлисти: {names}", False)
        tracks = [sp.simplify(t) for t in sp.playlist_items(p["id"])]
        return {"id": p["id"], "name": p["name"], "total": len(tracks), "tracks": tracks}
    except SpotifyError as e:
        return e.to_dict()
    except Exception as e:
        return err("internal_error", f"{type(e).__name__}: {e}")


@mcp.tool()
def save_playlist(name: str, track_uris: list[str], mode: str = "create",
                  description: str = "") -> dict:
    """WRITE operation. Create a new private playlist with the given tracks (mode="create")
    or append tracks to an existing playlist of the user (mode="append").

    Args:
        name: playlist name (1-100 chars). For append — name of the existing playlist.
        track_uris: list of "spotify:track:<22 chars>" URIs, 1-50 items.
        mode: "create" or "append".
        description: optional playlist description.

    Errors: invalid_input, invalid_uri, too_many_tracks, playlist_exists (create when the
    name is taken; hint suggests a free name), playlist_not_found (append), forbidden.
    Duplicate URIs and tracks already in the playlist are skipped and reported.
    """
    try:
        if ROLE == "planner":
            return err("forbidden", "Агент-планувальник не має права на запис.",
                       "Передай план агенту-виконавцю.")
        name = (name or "").strip()
        if not 1 <= len(name) <= 100:
            return err("invalid_input", "Назва має містити 1–100 символів.")
        if mode not in ("create", "append"):
            return err("invalid_input", f"Невідомий mode='{mode}'.", "Використай 'create' або 'append'.")
        if not isinstance(track_uris, list) or not track_uris:
            return err("empty_track_list", "Список track_uris порожній.",
                       "Спершу знайди треки через search_tracks.")
        bad = [u for u in track_uris if not isinstance(u, str) or not URI_RE.match(u)]
        if bad:
            return err("invalid_uri", f"Некоректні URI: {bad[:5]}",
                       "Формат: spotify:track:<22 символи>. Бери uri з результатів search_tracks.", True)

        unique, skipped = [], []
        for u in track_uris:
            (skipped if u in unique else unique).append(u)
        if len(unique) > MAX_TRACKS:
            return err("too_many_tracks", f"{len(unique)} треків, максимум {MAX_TRACKS}.",
                       "Зменш список.", True)

        existing = _find_playlist(name)
        if mode == "create":
            if existing is not None:
                base, n = existing["name"], 2
                taken = {(p.get("name") or "").lower() for p in sp.my_playlists()}
                while f"{base} ({n})".lower() in taken:
                    n += 1
                return err("playlist_exists",
                           f"Плейлист '{base}' вже існує.",
                           f"Щоб дописати треки — mode='append'. Щоб створити новий — назва '{base} ({n})'.",
                           True)
            created = sp.create_playlist(name, description)
            pid = created["id"]
            to_add = unique
        else:
            if existing is None:
                return err("playlist_not_found", f"Плейлист '{name}' не знайдено для append.",
                           "Перевір назву через get_playlist або використай mode='create'.", True)
            pid = existing["id"]
            already = {t["uri"] for t in sp.playlist_items(pid)}
            skipped += [u for u in unique if u in already]
            to_add = [u for u in unique if u not in already]

        if to_add:
            sp.add_items(pid, to_add)
        return {"ok": True, "mode": mode, "playlist_id": pid, "name": name,
                "added": len(to_add), "skipped_duplicates": skipped,
                "url": f"https://open.spotify.com/playlist/{pid}"}
    except SpotifyError as e:
        return e.to_dict()
    except Exception as e:
        return err("internal_error", f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    mcp.run()
