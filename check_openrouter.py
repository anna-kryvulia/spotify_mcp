from __future__ import annotations

import json
import os

import httpx
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
KEY = os.environ.get("OPENROUTER_API_KEY")
MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
BASE = "https://openrouter.ai/api/v1"


def main() -> None:
    if not KEY:
        raise SystemExit("OPENROUTER_API_KEY не задано в .env")
    h = {"Authorization": f"Bearer {KEY}"}

    print("== Ключ ==")
    print(json.dumps(httpx.get(f"{BASE}/key", headers=h, timeout=20).json().get("data"), indent=2))

    print("\n== Безкоштовні моделі з tool calling ==")
    models = httpx.get(f"{BASE}/models", params={"supported_parameters": "tools"}, timeout=30).json()["data"]
    free = sorted(m["id"] for m in models if m["id"].endswith(":free"))
    for mid in free:
        print(("→ " if mid == MODEL else "  ") + mid)
    if MODEL not in free:
        print(f"\n{MODEL} немає в списку. Обери модель зі списку й впиши в .env (OPENROUTER_MODEL).")

    print(f"\n== Тестовий tool call: {MODEL} ==")
    client = OpenAI(base_url=BASE, api_key=KEY)
    resp = client.chat.completions.create(
        model=MODEL, temperature=0,
        messages=[{"role": "user", "content": "Find tracks by Radiohead. Use the tool."}],
        tools=[{"type": "function", "function": {
            "name": "search_tracks", "description": "Search tracks by artist",
            "parameters": {"type": "object", "properties": {"artist": {"type": "string"}},
                           "required": ["artist"]}}}],
    )
    calls = resp.choices[0].message.tool_calls
    if calls:
        print("Модель викликала:", calls[0].function.name, calls[0].function.arguments)
    else:
        print("Модель відповіла текстом без tool call:", (resp.choices[0].message.content or "")[:200])


if __name__ == "__main__":
    main()
