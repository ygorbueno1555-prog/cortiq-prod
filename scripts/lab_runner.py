"""lab_runner.py — Async wrapper do experiment_engine para SSE streaming."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import AsyncIterator

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))


async def run_experiment_stream(
    candidates: int = 3,
    dry_run: bool = True,
    mutation_type: str | None = None,
) -> AsyncIterator[str]:
    """Roda experiment_engine em thread pool, emite SSE strings."""
    from experiment_engine import run_experiment

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_progress(event: str, data: dict):
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def _run():
        try:
            result = run_experiment(
                mutation_type=mutation_type,
                candidates=candidates,
                dry_run=dry_run,
                on_progress=on_progress,
            )
            loop.call_soon_threadsafe(queue.put_nowait, ("__done__", result))
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("__error__", {"message": str(exc)}))

    loop.run_in_executor(None, _run)

    while True:
        event, data = await queue.get()
        sse_data = json.dumps(data, ensure_ascii=False)
        yield f"event: {event}\ndata: {sse_data}\n\n"
        if event in ("__done__", "__error__"):
            break
