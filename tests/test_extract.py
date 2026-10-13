"""One end-to-end extraction scenario for the generic forwarding API."""

import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.extraction import ExtractRequest
from app.services.extraction import ExtractionService
from app.providers.openai import OpenAIProvider


def test_extract_with_caller_prompt_and_model_override(monkeypatch, caplog):
    calls = []
    streams = []
    delay = 0
    finish_override = None
    emitted = []
    active = 0
    peak = 0

    class ModelStream:
        def __init__(self, content, reasoning, finish_reason):
            self.content = content
            self.reasoning = reasoning
            self.finish_reason = finish_reason
            self.closed = False
            streams.append(self)

        async def __aenter__(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            return self

        async def __aexit__(self, *args):
            nonlocal active
            active -= 1
            self.closed = True

        async def __aiter__(self):
            def chunk(content=None, finish_reason=None, **reasoning):
                return SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=content, **reasoning),
                    finish_reason=finish_reason,
                )])

            await asyncio.sleep(delay)
            for key, value in self.reasoning.items():
                for part in ("Identify", " ", value[len("Identify "):]):
                    yield chunk(**{key: part})
            midpoint = len(self.content) // 2
            emitted.append(self.content[:midpoint])
            yield chunk(content=self.content[:midpoint])
            await asyncio.sleep(0.01)
            # The consumer must log chunks before waiting for subsequent output.
            assert "Model stream [" in caplog.text
            emitted.append(self.content[midpoint:])
            yield chunk(content=self.content[midpoint:])
            yield chunk(finish_reason=self.finish_reason)
            # Usage-only chunks have no choices and must not discard the result.
            yield SimpleNamespace(choices=[])

    async def completion(**kwargs):
        calls.append(kwargs)
        text = json.loads(kwargs["messages"][-1]["content"])["text"]
        content = json.dumps({"names": ["Taylor Swift"] if "Taylor" in text else []})
        reasoning = {}
        if kwargs.get("reasoning_effort") == "medium":
            key = "reasoning" if "Taylor" in text else "reasoning_content"
            reasoning[key] = "Identify people and exclude company names."
        finish_reason = finish_override or ("length" if kwargs.get("max_tokens") == 1 else "stop")
        if kwargs["stream"]:
            return ModelStream(content, reasoning, finish_reason)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason, message=SimpleNamespace(content=content, **reasoning),
        )])

    app = create_app(Settings(
        base_url="http://model.test/v1",
        model="default-model",
        system_prompt="Return one JSON object and follow the caller task.",
        concurrency=2,
        api_key="test-secret-key",
        debug=True,
    ))
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.extractor.provider.client.chat.completions,
            "create", AsyncMock(side_effect=completion),
        )
        response = client.post("/extract", json={
            "prompt": "Extract person names only. Return {names: string[]}.",
            "text": "Taylor Swift announces a new album",
            "examples": [{
                "text": "Taylor Swift wins an award",
                "result": {"names": ["Taylor Swift"]},
            }],
            "model": "stronger-model",
            "max_tokens": 256,
            "reasoning_effort": "medium",
        })
        assert len(calls) == 1
        invalid = client.post("/extract", json={
            "prompt": "Extract names as JSON.", "texts": ["Taylor Swift"],
        })
        assert invalid.status_code == 422
        assert len(calls) == 1
        for override in ({}, {"reasoning_effort": "none"}, {"reasoning_effort": None, "max_tokens": None}):
            followup = client.post("/extract", json={
                "prompt": "Extract person names only as JSON.",
                "text": "Taylor Swift announces a new album",
                **override,
            })
            assert followup.status_code == 200
            assert "reasoning" not in followup.json()

        app.state.extractor.provider.debug = False
        non_streaming = client.post("/extract", json={
            "prompt": "Extract person names only as JSON.",
            "text": "Taylor Swift announces a new album",
        })
        assert non_streaming.status_code == 200
        assert non_streaming.json()["data"] == {"names": ["Taylor Swift"]}

        app.state.extractor.provider.debug = True
        truncated = client.post("/extract", json={
            "prompt": "Extract person names only as JSON.",
            "text": "Taylor Swift announces a new album",
            "max_tokens": 1,
        })
        assert truncated.status_code == 502
        assert "truncated" in truncated.json()["detail"]

        app.state.extractor.provider.debug = False
        monkeypatch.setattr("app.services.extraction.HEARTBEAT_INTERVAL", 0.01)
        delay = 0.03
        streamed = client.post("/extract", json={
            "prompt": "Extract person names only as JSON.",
            "text": "Taylor Swift announces a new album",
            "reasoning_effort": "medium", "stream": True,
        })
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        assert streamed.headers["x-accel-buffering"] == "no"
        assert ": ping\n\n" in streamed.text

        def parse_events(text):
            return [
                (block.splitlines()[0].removeprefix("event: "), json.loads(block.splitlines()[1].removeprefix("data: ")))
                for block in text.split("\n\n") if block.startswith("event: ")
            ]

        events = parse_events(streamed.text)
        assert events[0][0] == "start" and events[-1][0] == "done"
        assert "".join(data.get("content", "") for name, data in events if name == "delta") == '{"names": ["Taylor Swift"]}'
        assert "".join(data.get("reasoning", "") for name, data in events if name == "delta") == "Identify people and exclude company names."
        result_event = next(data for name, data in events if name == "result")
        assert "index" not in result_event
        assert all("index" not in data and "count" not in data for _, data in events)
        assert result_event["model"] == "default-model"
        assert result_event["data"] == {"names": ["Taylor Swift"]}
        assert result_event["reasoning"] == "Identify people and exclude company names."

        finish_override = "length"
        failed = client.post("/extract", json={
            "prompt": "Extract person names only as JSON.",
            "text": "Taylor Swift announces a new album", "stream": True,
        })
        failed_events = parse_events(failed.text)
        assert failed_events[-1][0] == "error"
        assert failed_events[-1][1]["status_code"] == 502
        assert all(name != "done" for name, _ in failed_events)
        finish_override = None
        delay = 0

    assert response.status_code == 200
    assert response.json() == {
        "model": "stronger-model",
        "text": "Taylor Swift announces a new album",
        "data": {"names": ["Taylor Swift"]},
        "reasoning": "Identify people and exclude company names.",
    }
    assert len(calls) == 8
    assert calls[0]["model"] == "stronger-model"
    assert calls[0]["max_tokens"] == 256
    assert all("max_tokens" not in call for call in calls[1:5])
    assert calls[0]["reasoning_effort"] == "medium"
    assert calls[1]["reasoning_effort"] == "none"
    assert calls[2]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in calls[3]
    assert calls[4]["reasoning_effort"] == "none"
    assert calls[5]["reasoning_effort"] == "none"
    assert all(call["response_format"] == {"type": "json_object"} for call in calls)
    assert calls[0]["messages"][0]["content"].endswith(
        "Task:\nExtract person names only. Return {names: string[]}."
    )
    assert json.loads(calls[0]["messages"][2]["content"]) == {"names": ["Taylor Swift"]}
    assert "POST /extract:" in caplog.text
    assert "Model request:" in caplog.text
    assert "Taylor Swift announces a new album" in caplog.text
    assert "test-secret-key" not in caplog.text
    assert all(call["stream"] for call in calls[:4])
    assert calls[4]["stream"] is False
    assert len(streams) == 7
    assert all(stream.closed for stream in streams)
    assert '"reasoning": "Identify"' in caplog.text
    assert '"finish_reason": "stop"' in caplog.text
    assert '"finish_reason": "length"' in caplog.text

    async def verify_live_delivery_and_disconnect():
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
        service = ExtractionService(OpenAIProvider(sdk, debug=True), Settings(
            base_url="http://model.test/v1", model="default-model", concurrency=2,
        ))
        stream = service.stream(ExtractRequest(
            prompt="Extract names as JSON.", text="Taylor Swift announces a new album", stream=True,
        ))
        await anext(stream)
        emitted.clear()
        event = await anext(stream)
        assert event["event"] == "delta"
        assert len(emitted) == 1
        await stream.aclose()
        assert all(item.closed for item in streams)
        request = ExtractRequest(prompt="Extract names as JSON.", text="Taylor Swift")
        responses = await asyncio.gather(*(service.extract(request) for _ in range(4)))
        assert all(response.data == {"names": ["Taylor Swift"]} for response in responses)
        assert peak == 2 and active == 0

    asyncio.run(verify_live_delivery_and_disconnect())
