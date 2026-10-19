"""
evaluate.py — 5 тестових запитів, автоматична перевірка результату, порівняння multi vs single.

Як працює:
  1. для кожного режиму (multi, single): reset.py прибирає тестові плейлисти;
  2. запити виконуються ПО ПОРЯДКУ (тест 2 залежить від 1, тест 4 — від 3);
  3. після кожного запиту checker дивиться на реальний стан Spotify (а не на слова агента)
     і ставить PASS/FAIL з поясненням;
  4. результат — таблиця в консолі + results/eval_<час>.json.

Checker звертається до Spotify напряму: це «суддя», а не агент.

Запуск:
  uv run evaluate.py                 — обидва режими (≈50–70 запитів до LLM!)
  uv run evaluate.py --mode multi    — лише одна система
  uv run evaluate.py --only 1 2      — лише вибрані тести (без reset, якщо не 1-й)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from rich.table import Table

import spotify_client as sp
from agents import MODEL, run
from reset import reset
from tracing import console

RESULTS_DIR = Path(__file__).resolve().parent / "results"


# --------------------------------------------------------------------------- helpers

def find(name: str) -> dict | None:
    for p in sp.my_playlists():
        if (p.get("name") or "").strip().lower() == name.lower():
            return p
    return None


def tracks_of(name: str) -> list[dict] | None:
    p = find(name)
    return None if p is None else [sp.simplify(t) for t in sp.playlist_items(p["id"])]


def by_artist(tracks: list[dict], artist: str) -> list[dict]:
    return [t for t in tracks if artist.lower() in [a.lower() for a in t["artists"]]]


def has_word(tracks: list[dict], word: str) -> list[str]:
    rx = re.compile(rf"\b{word}\b", re.I)
    return [t["name"] for t in tracks if rx.search(t["name"] or "") or rx.search(t["album"] or "")]


# --------------------------------------------------------------------------- checks
# Кожна перевірка повертає (passed: bool, reason: str).

def check_1():
    t = tracks_of("Radiohead Essentials")
    if t is None:
        return False, "плейлист не створено"
    if len(t) != 5:
        return False, f"{len(t)} треків замість 5"
    if len(by_artist(t, "Radiohead")) != 5:
        return False, "є треки не від Radiohead"
    if live := has_word(t, "live"):
        return False, f"live-треки: {live}"
    return True, "5 треків Radiohead без live"


def check_2():
    orig, new = tracks_of("Radiohead Essentials"), tracks_of("Radiohead Essentials (2)")
    if orig is None:
        return False, "оригінальний плейлист зник"
    if len(orig) != 5:
        return False, f"оригінал змінився: {len(orig)} треків (дубль через append?)"
    if new is None:
        return False, "після playlist_exists агент не створив 'Radiohead Essentials (2)'"
    if len(new) != 5 or has_word(new, "live"):
        return False, f"новий плейлист некоректний: {len(new)} треків"
    return True, "конфлікт назви оброблено, створено (2)"


def check_3():
    t = tracks_of("French Touch")
    if t is None:
        return False, "плейлист не створено"
    dp, ju = len(by_artist(t, "Daft Punk")), len(by_artist(t, "Justice"))
    if (dp, ju, len(t)) != (3, 3, 6):
        return False, f"Daft Punk={dp}, Justice={ju}, всього={len(t)} (треба 3/3/6)"
    if rm := has_word(t, "remix"):
        return False, f"ремікси: {rm}"
    return True, "3 Daft Punk + 3 Justice без реміксів"


def check_4():
    t = tracks_of("French Touch")
    if t is None:
        return False, "плейлиста French Touch немає (тест 3 провалився?)"
    ma = len(by_artist(t, "Massive Attack"))
    if len(t) != 8 or ma != 2:
        return False, f"всього={len(t)}, Massive Attack={ma} (треба 8 і 2)"
    if find("French Touch (2)") is not None:
        return False, "створено новий плейлист замість append"
    return True, "дописано 2 треки Massive Attack"


def check_5():
    bad = [p["name"] for p in sp.my_playlists() if "xqzzyt" in (p.get("name") or "").lower()]
    if bad:
        return False, f"створено плейлист для неіснуючого гурту: {bad}"
    return True, "нічого не створено"


TESTS = [
    (1, "Створи плейлист з 5 пісень Radiohead без live-версій, назви його Radiohead Essentials", check_1),
    (2, "Створи плейлист з 5 пісень Radiohead без live-версій, назви його Radiohead Essentials", check_2),
    (3, "Створи плейлист French Touch: 3 треки Daft Punk і 3 треки Justice, без реміксів", check_3),
    (4, "Додай 2 пісні Massive Attack у мій плейлист French Touch", check_4),
    (5, "Створи плейлист з 10 пісень гурту Xqzzyt Brothers", check_5),
]


# --------------------------------------------------------------------------- runner

async def evaluate(modes: list[str], only: list[int] | None, pause: int) -> dict:
    report = {"model": MODEL, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "modes": {}}
    for mode in modes:
        console.rule(f"[bold]РЕЖИМ: {mode}")
        if not only or 1 in only:
            reset()
        rows = []
        for tid, query, check in TESTS:
            if only and tid not in only:
                continue
            res = await run(query, mode, label=f"{mode}-t{tid}")
            try:
                passed, reason = check()
            except sp.SpotifyError as e:
                passed, reason = False, f"checker: {e.message}"
            rows.append({"test": tid, "query": query, "passed": passed, "reason": reason, **res})
            console.print(f"[bold]{'PASS' if passed else 'FAIL'}[/] тест {tid}: {reason}\n")
            await asyncio.sleep(pause)  # 20 запитів/хв на безкоштовних моделях
        report["modes"][mode] = {"passed": sum(r["passed"] for r in rows), "total": len(rows), "rows": rows}
    return report


def print_table(report: dict) -> None:
    modes = list(report["modes"])
    table = Table(title=f"Результати · {report['model']}")
    table.add_column("Тест")
    for m in modes:
        table.add_column(f"{m}: результат")
        table.add_column(f"{m}: LLM / tools")
    tests = sorted({r["test"] for m in modes for r in report["modes"][m]["rows"]})
    for tid in tests:
        cells = [str(tid)]
        for m in modes:
            r = next((x for x in report["modes"][m]["rows"] if x["test"] == tid), None)
            cells += (["—", "—"] if r is None else
                      [("✅ " if r["passed"] else "❌ ") + r["reason"],
                       f"{r['llm_calls']} / {r['tool_calls']}"])
        table.add_row(*cells)
    total = ["Разом"]
    for m in modes:
        d = report["modes"][m]
        llm = sum(r["llm_calls"] for r in d["rows"])
        total += [f"{d['passed']}/{d['total']}", f"{llm} LLM"]
    table.add_row(*total, style="bold")
    console.print(table)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["multi", "single", "both"], default="both")
    ap.add_argument("--only", type=int, nargs="*")
    ap.add_argument("--pause", type=int, default=5, help="пауза між тестами, с")
    a = ap.parse_args()

    modes = ["multi", "single"] if a.mode == "both" else [a.mode]
    rep = asyncio.run(evaluate(modes, a.only, a.pause))
    print_table(rep)
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"Збережено: {out}")
