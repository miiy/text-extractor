"""Single-text extraction with a shared limit on concurrent model calls."""

import asyncio
import json
import logging
from typing import Protocol

from app.core.config import Settings
from app.providers.openai import ChatResult, ModelError, ModelTimeout, parse_result
from app.schemas.extraction import ExtractRequest, ExtractResponse

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = 15


class ChatProvider(Protocol):
    async def chat(self, messages, model: str, max_tokens: int | None, temperature: float, reasoning_effort: str | None = None, on_delta=None) -> ChatResult: ...


class ExtractionService:
    def __init__(self, provider: ChatProvider, settings: Settings):
        self.provider = provider
        self.settings = settings
        self.semaphore = asyncio.Semaphore(settings.concurrency)

    async def extract(self, request: ExtractRequest, on_delta=None) -> ExtractResponse:
        model = request.model or self.settings.model
        examples = request.examples or []
        messages = [{"role": "system", "content": self.settings.system_prompt + "\n\nTask:\n" + request.prompt}]
        for example in examples:
            messages.extend([
                {"role": "user", "content": json.dumps({"text": example.text}, ensure_ascii=False)},
                {"role": "assistant", "content": json.dumps(example.result, ensure_ascii=False, allow_nan=False)},
            ])

        async with self.semaphore:
            result = await self.provider.chat(
                messages + [{"role": "user", "content": json.dumps({"text": request.text}, ensure_ascii=False)}],
                model,
                request.max_tokens,
                request.temperature,
                reasoning_effort=request.reasoning_effort,
                on_delta=on_delta,
            )
        fields = {"reasoning": result.reasoning} if result.reasoning else {}
        return ExtractResponse(model=model, text=request.text, data=parse_result(result.content), **fields)

    async def stream(self, request: ExtractRequest):
        model = request.model or self.settings.model
        queue = asyncio.Queue(maxsize=256)

        async def on_delta(content, reasoning):
            data = {}
            if content:
                data["content"] = content
            if reasoning:
                data["reasoning"] = reasoning
            await queue.put({"event": "delta", "data": data})

        async def produce():
            try:
                result = await self.extract(request, on_delta=on_delta)
                await queue.put({"event": "result", "data": result.model_dump(exclude_unset=True)})
            except ModelTimeout as exc:
                await queue.put({"event": "error", "data": {"status_code": 504, "detail": str(exc)}})
            except ModelError as exc:
                await queue.put({"event": "error", "data": {"status_code": 502, "detail": str(exc)}})
            except Exception:
                logger.exception("Unexpected extraction stream error")
                await queue.put({"event": "error", "data": {"status_code": 500, "detail": "Extraction failed"}})
            else:
                await queue.put({"event": "done", "data": {"model": model}})

        yield {"event": "start", "data": {"model": model}}
        producer = asyncio.create_task(produce())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                except TimeoutError:
                    yield {"event": "ping", "data": {}}
                    continue
                yield event
                if event["event"] in {"done", "error"}:
                    break
        finally:
            # Disconnects stop queued work and close active upstream streams.
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
