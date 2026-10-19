"""
reset.py — прибирає тестові плейлисти, щоб кожен прогін evaluate.py стартував з однакового стану.

Spotify не має «видалення» плейлиста: власник відписується від нього,
і плейлист зникає з бібліотеки. Після змін лютого 2026 це робиться через
DELETE /me/library?uris=spotify:playlist:<id>.

Це тестова обв'язка, а не агент, тому вона звертається до Spotify напряму
(через spotify_client), без MCP.

Запуск:  uv run reset.py            — прибрати плейлисти з TEST_PLAYLISTS
         uv run reset.py --dry-run  — лише показати, що буде прибрано
"""

from __future__ import annotations

import argparse

import spotify_client as sp

# Точні назви (без урахування регістру) + підрядки, які означають тестовий плейлист.
TEST_PLAYLISTS = {"radiohead essentials", "radiohead essentials (2)", "radiohead essentials (3)",
                  "french touch", "french touch (2)"}
TEST_SUBSTRINGS = ("xqzzyt",)


def is_test(name: str) -> bool:
    n = (name or "").strip().lower()
    return n in TEST_PLAYLISTS or any(s in n for s in TEST_SUBSTRINGS)


def reset(dry_run: bool = False) -> list[str]:
    targets = [p for p in sp.my_playlists() if is_test(p.get("name"))]
    for p in targets:
        print(("[dry-run] " if dry_run else "") + f"прибираю: {p['name']} ({p['id']})")
    if targets and not dry_run:
        uris = [f"spotify:playlist:{p['id']}" for p in targets]
        for i in range(0, len(uris), 40):  # максимум 40 URI за запит
            sp.remove_from_library(uris[i:i + 40])
    if not targets:
        print("Тестових плейлистів немає.")
    return [p["name"] for p in targets]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    reset(ap.parse_args().dry_run)
