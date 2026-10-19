"""
tracing.py — трейсинг агентної системи без зовнішніх сервісів.

Кожна подія (виклик LLM, виклик інструмента, результат, передача між агентами)
пишеться одним JSON-рядком у traces/traces.jsonl і паралельно друкується
в консоль кольором свого агента. Консоль — для відео-демо, JSONL — для аналізу
помилок після evaluate.py (наприклад, `grep '"run_id": "..."' traces/traces.jsonl`).
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from rich.console import Console

TRACE_DIR = Path(__file__).resolve().parent / "traces"
COLORS = {"planner": "cyan", "executor": "magenta", "single": "yellow", "system": "white"}

console = Console()


class Tracer:
    def __init__(self, label: str = ""):
        TRACE_DIR.mkdir(exist_ok=True)
        self.run_id = uuid.uuid4().hex[:8]
        self.label = label
        self.path = TRACE_DIR / "traces.jsonl"
        self.counters = {"llm_calls": 0, "tool_calls": 0, "tool_errors": 0}
        self._t0 = time.time()

    def event(self, kind: str, agent: str = "system", **data) -> None:
        if kind == "llm_call":
            self.counters["llm_calls"] += 1
        elif kind == "tool_call":
            self.counters["tool_calls"] += 1
        elif kind == "tool_result" and data.get("error"):
            self.counters["tool_errors"] += 1

        record = {"ts": round(time.time(), 3), "run_id": self.run_id, "label": self.label,
                  "agent": agent, "kind": kind, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._print(kind, agent, data)

    def elapsed(self) -> float:
        return round(time.time() - self._t0, 1)

    # ---- консольний вивід
    def _print(self, kind: str, agent: str, d: dict) -> None:
        c = COLORS.get(agent, "white")
        tag = f"[bold {c}]{agent:>8}[/]"
        if kind == "run_start":
            console.rule(f"[bold]run {self.run_id}[/] · {d.get('mode')} · {d.get('query')}")
        elif kind == "llm_call":
            console.print(f"{tag} 🧠 LLM крок {d.get('step')} · {d.get('latency_s')} с")
        elif kind == "tool_call":
            console.print(f"{tag} 🔧 [bold]{d.get('tool')}[/]({_short(d.get('args'), 160)})")
        elif kind == "tool_result":
            if d.get("error"):
                console.print(f"{tag}    [red]✗ {d.get('error')}: {d.get('message')}[/]")
                if d.get("hint"):
                    console.print(f"{tag}    [red]  hint: {d.get('hint')}[/]")
            else:
                console.print(f"{tag}    [green]✓[/] {_short(d.get('preview'), 160)} · {d.get('ms')} мс")
        elif kind == "handoff":
            console.print(f"[bold white]  ↪ {d.get('from')} → {d.get('to')}[/]: {_short(d.get('payload'), 200)}")
        elif kind == "agent_output":
            console.print(f"{tag} 💬 {_short(d.get('text'), 300)}")
        elif kind == "run_end":
            console.print(f"[bold]  ■ кінець · {self.elapsed()} с · {self.counters}[/]")
        else:
            console.print(f"{tag} {kind}: {_short(d, 200)}")


def _short(x, n: int) -> str:
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"
