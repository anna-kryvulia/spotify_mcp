"""
agents.py — два агенти (Планувальник + Виконавець) і базовий варіант з одним агентом.

Як влаштовано:
  * MCPToolbox — MCP-клієнт. Кожен агент запускає СВІЙ підпроцес server.py
    через stdio і бачить лише дозволені йому інструменти. Агенти не імпортують
    функції сервера — усі виклики йдуть через ClientSession.call_tool().
  * Agent — простий цикл tool calling: LLM (OpenRouter, OpenAI-сумісний API)
    → виклики інструментів через MCP → результати назад у LLM → … → фінальна відповідь.
  * run_multi — Планувальник складає JSON-план (лише читання), Виконавець перевіряє
    його проти запиту й записує. Якщо план поганий — один раунд повернення на доопрацювання.
  * run_single — один агент з усіма трьома інструментами (для порівняння в evaluate.py).

Запуск:
  uv run agents.py "Створи плейлист з 5 пісень Radiohead без live-версій, назви Radiohead Essentials"
  uv run agents.py "..." --mode single
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import APIStatusError, AsyncOpenAI, RateLimitError

from tracing import Tracer

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
SERVER = BASE_DIR / "server.py"

READ_TOOLS = {"search_tracks", "get_playlist"}
PLANNER_TOOLS = {"search_tracks", "get_playlist"}
EXECUTOR_TOOLS = {"get_playlist", "save_playlist"}
SINGLE_TOOLS = {"search_tracks", "get_playlist", "save_playlist"}

# --------------------------------------------------------------------------- prompts
# Промпти англійською: безкоштовні моделі стабільніше виконують інструкції англійською.
# Відповідь користувачу (поле note) — українською.

PLANNER_PROMPT = """You are the PLANNER in a Spotify playlist system.
You can only READ data (tools: search_tracks, get_playlist). You can NOT create or change playlists.

Turn the user's request into a precise plan:
1. Extract: playlist name (invent a short one if none is given); mode = "create", or "append" only if the
   user asks to add tracks to an existing playlist; artists and how many tracks per artist; words to exclude
   (e.g. "без live-версій" -> ["live"], "без реміксів" -> ["remix"]).
2. Call search_tracks once per artist, with `exclude` and limit = requested count + 3.
3. For "append": call get_playlist to confirm the playlist exists and avoid tracks already in it.
4. Choose EXACTLY the requested number of tracks per artist. Re-check each chosen title and album
   against the excluded words.
5. If search_tracks returns no_tracks_found for an artist, do NOT invent tracks: return status "impossible".

Reply with ONLY one JSON object, no prose, no markdown:
{"status": "ok" | "impossible", "name": "...", "mode": "create" | "append",
 "requirements": {"per_artist": {"<Artist>": <n>}, "exclude": ["..."]},
 "tracks": [{"uri": "spotify:track:...", "name": "...", "artist": "..."}],
 "reason": "<only when impossible>"}
If the message contains FEEDBACK FROM EXECUTOR, fix exactly those problems and return a new full plan."""

EXECUTOR_PROMPT = """You are the EXECUTOR/VALIDATOR in a Spotify playlist system.
You are the ONLY agent allowed to write (tools: get_playlist, save_playlist).
You receive the ORIGINAL user request and a JSON plan from the planner.

1. Validate the plan against the ORIGINAL request (not only against the plan's own "requirements"):
   - number of tracks per artist equals what the user asked;
   - no track or album title contains an excluded word (whole word, case-insensitive);
   - no duplicate URIs; mode is "append" only if the user asked to add to an existing playlist.
2. If plan status is "impossible": do not write. Reply {"verdict": "impossible", "reason": "..."}.
3. If the plan breaks a rule: do not write. Reply {"verdict": "reject", "reason": "<concrete problem>"}.
4. Otherwise call save_playlist. If it returns an error with "retryable": true, follow its "hint" ONCE
   (e.g. playlist_exists on create -> create again with the free name from the hint; use append only
   if the user explicitly asked to add to the existing playlist).
5. After a successful write call get_playlist and check the number of tracks.

Final reply: ONLY one JSON object, no prose, no markdown:
{"verdict": "done" | "reject" | "impossible" | "error", "playlist": "<name>",
 "tracks_in_playlist": <n>, "reason": "<for reject/impossible/error>",
 "note": "<one short sentence for the user in Ukrainian>"}"""

SINGLE_PROMPT = """You are a Spotify playlist assistant with tools search_tracks, get_playlist, save_playlist.

1. Extract: playlist name (invent a short one if none is given); mode = "create", or "append" only if the
   user asks to add tracks to an existing playlist; artists and number of tracks per artist; excluded words.
2. Call search_tracks once per artist (with `exclude`, limit = requested count + 3). For append, call
   get_playlist first.
3. Choose exactly the requested number of tracks per artist; check titles and albums for excluded words.
4. If an artist has no tracks (no_tracks_found), do NOT create anything; reply with verdict "impossible".
5. Call save_playlist. If it returns an error with "retryable": true, follow its "hint" ONCE.
6. After a successful write, call get_playlist and check the number of tracks.

Final reply: ONLY one JSON object, no prose, no markdown:
{"verdict": "done" | "impossible" | "error", "playlist": "<name>", "tracks_in_playlist": <n>,
 "reason": "<if not done>", "note": "<one short sentence for the user in Ukrainian>"}"""


# --------------------------------------------------------------------------- MCP client

class MCPToolbox:
    """MCP-клієнт для одного агента: свій підпроцес сервера + фільтр дозволених інструментів."""

    def __init__(self, role: str, allowed: set[str]):
        self.role, self.allowed = role, allowed
        self.tools: list = []
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "MCPToolbox":
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], cwd=str(BASE_DIR),
            env={**os.environ, "AGENT_ROLE": self.role},  # серверний рубіж прав запису
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = [t for t in listed.tools if t.name in self.allowed]  # клієнтський рубіж
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    def openai_schema(self) -> list[dict]:
        """MCP tool -> формат tools для OpenAI-сумісного API."""
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description or "",
                              "parameters": t.inputSchema}} for t in self.tools]

    async def call(self, name: str, args: dict) -> dict:
        if name not in self.allowed:
            return {"error": "forbidden", "message": f"Агент '{self.role}' не має доступу до {name}.",
                    "hint": f"Доступні інструменти: {sorted(self.allowed)}", "retryable": False}
        result = await self.session.call_tool(name, args)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.isError:  # напр. помилка валідації аргументів на рівні MCP
            return {"error": "tool_call_failed", "message": text[:500],
                    "hint": "Перевір назви й типи аргументів за схемою інструмента.", "retryable": True}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text[:2000]}


# --------------------------------------------------------------------------- agent loop

class Agent:
    def __init__(self, name: str, prompt: str, toolbox: MCPToolbox, tracer: Tracer,
                 llm: AsyncOpenAI, max_steps: int = 8):
        self.name, self.prompt, self.toolbox = name, prompt, toolbox
        self.tracer, self.llm, self.max_steps = tracer, llm, max_steps

    async def run(self, user_msg: str) -> str:
        messages = [{"role": "system", "content": self.prompt},
                    {"role": "user", "content": user_msg}]
        tools = self.toolbox.openai_schema()

        for step in range(1, self.max_steps + 1):
            msg = await self._complete(messages, tools, step)

            if not msg.tool_calls:
                text = msg.content or ""
                self.tracer.event("agent_output", self.name, text=text)
                return text

            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": tc.id, "type": "function",
                                             "function": {"name": tc.function.name,
                                                          "arguments": tc.function.arguments or "{}"}}
                                            for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args, result = {}, {"error": "bad_arguments", "retryable": True,
                                        "message": "Аргументи не є валідним JSON.",
                                        "hint": "Передай аргументи як JSON-об'єкт."}
                else:
                    result = None
                self.tracer.event("tool_call", self.name, tool=name, args=args)
                t0 = time.time()
                if result is None:
                    result = await self.toolbox.call(name, args)
                ms = int((time.time() - t0) * 1000)
                if isinstance(result, dict) and "error" in result:
                    self.tracer.event("tool_result", self.name, tool=name, ms=ms, **result)
                else:
                    self.tracer.event("tool_result", self.name, tool=name, ms=ms,
                                      preview=json.dumps(result, ensure_ascii=False)[:300])
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})

        self.tracer.event("agent_output", self.name, text="max_steps reached")
        return json.dumps({"verdict": "error", "reason": f"{self.name}: досягнуто max_steps"})

    async def _complete(self, messages, tools, step):
        """Виклик LLM з обробкою лімітів безкоштовних моделей OpenRouter."""
        for attempt in range(4):
            t0 = time.time()
            try:
                resp = await self.llm.chat.completions.create(
                    model=MODEL, messages=messages, tools=tools, tool_choice="auto", temperature=0)
            except RateLimitError:
                wait = 15 * 2 ** attempt
                self.tracer.event("llm_rate_limited", self.name, wait_s=wait)
                await asyncio.sleep(wait)
                continue
            except APIStatusError as e:
                if e.status_code == 404 and "tool" in str(e).lower():
                    raise RuntimeError(f"Модель {MODEL} не підтримує tool calling. "
                                       "Обери іншу через check_openrouter.py.") from e
                raise
            self.tracer.event("llm_call", self.name, step=step, model=MODEL,
                              latency_s=round(time.time() - t0, 1),
                              usage=resp.usage.model_dump() if resp.usage else None)
            if resp.choices:
                return resp.choices[0].message
            await asyncio.sleep(5)  # порожня відповідь від перевантаженого провайдера
        raise RuntimeError("LLM недоступна: вичерпано повтори (ліміт OpenRouter або перевантаження).")


# --------------------------------------------------------------------------- pipelines

def parse_json(text: str) -> dict:
    """Дістає перший JSON-об'єкт з відповіді моделі (моделі іноді додають ```json)."""
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"verdict": "unparseable", "raw": text[:500]}


def make_llm() -> AsyncOpenAI:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY не задано в .env")
    return AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)


async def run_multi(query: str, tracer: Tracer, llm: AsyncOpenAI) -> dict:
    async with MCPToolbox("planner", PLANNER_TOOLS) as pbox, \
               MCPToolbox("executor", EXECUTOR_TOOLS) as ebox:
        planner = Agent("planner", PLANNER_PROMPT, pbox, tracer, llm)
        executor = Agent("executor", EXECUTOR_PROMPT, ebox, tracer, llm)

        plan = await planner.run(query)
        verdict: dict = {}
        for attempt in range(2):
            tracer.event("handoff", **{"from": "planner", "to": "executor", "payload": plan})
            out = await executor.run(f"ORIGINAL USER REQUEST:\n{query}\n\nPLAN FROM PLANNER:\n{plan}")
            verdict = parse_json(out)
            if verdict.get("verdict") == "reject" and attempt == 0:
                tracer.event("handoff", **{"from": "executor", "to": "planner",
                                           "payload": verdict.get("reason", "")})
                plan = await planner.run(f"{query}\n\nFEEDBACK FROM EXECUTOR: {verdict.get('reason')}"
                                         f"\n\nYOUR PREVIOUS PLAN:\n{plan}")
                continue
            break
        return verdict


async def run_single(query: str, tracer: Tracer, llm: AsyncOpenAI) -> dict:
    async with MCPToolbox("single", SINGLE_TOOLS) as box:
        agent = Agent("single", SINGLE_PROMPT, box, tracer, llm, max_steps=10)
        return parse_json(await agent.run(query))


async def run(query: str, mode: str = "multi", label: str = "") -> dict:
    tracer = Tracer(label=label or mode)
    tracer.event("run_start", mode=mode, query=query, model=MODEL)
    llm = make_llm()
    try:
        fn = run_multi if mode == "multi" else run_single
        verdict = await fn(query, tracer, llm)
    except Exception as e:  # падіння LLM/мережі фіксуємо в трейсі, а не губимо
        verdict = {"verdict": "crash", "reason": f"{type(e).__name__}: {e}"}
    tracer.event("run_end", verdict=verdict, **tracer.counters)
    return {"run_id": tracer.run_id, "verdict": verdict, "seconds": tracer.elapsed(), **tracer.counters}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--mode", choices=["multi", "single"], default="multi")
    a = ap.parse_args()
    print(json.dumps(asyncio.run(run(a.query, a.mode)), ensure_ascii=False, indent=2))
