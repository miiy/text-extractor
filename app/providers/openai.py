"""Chat provider for OpenAI-compatible model services."""

import json
import logging
import math
import re
from dataclasses import dataclass
from uuid import uuid4

import openai

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatResult:
    content: str
    reasoning: str | None = None


class ModelError(Exception):
    """The model call failed or returned content outside the JSON contract."""


class ModelTimeout(ModelError):
    """The model request timed out."""


class OpenAIProvider:
    def __init__(self, client: openai.AsyncOpenAI, debug: bool = False, omit_auth: bool = False):
        self.client = client
        self.debug = debug
        self.omit_auth = omit_auth

    async def chat(self, messages, model, max_tokens, temperature, reasoning_effort=None, on_delta=None):
        payload = {
            "model": model,
            "messages": messages,
            "stream": self.debug or on_delta is not None,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        request_id = uuid4().hex[:8]
        logger.debug("Model request: [%s] %s", request_id, json.dumps(payload, ensure_ascii=False))
        options = {"extra_headers": {"Authorization": openai.Omit()}} if self.omit_auth else {}
        try:
            response = await self.client.chat.completions.create(**payload, **options)
            if payload["stream"]:
                return await self._read_stream(response, request_id, on_delta)
        except openai.APITimeoutError as exc:
            raise ModelTimeout("The model service request timed out") from exc
        except openai.APIStatusError as exc:
            raise ModelError(f"The model service returned HTTP {exc.status_code}") from exc
        except openai.APIConnectionError as exc:
            raise ModelError("Unable to connect to the model service") from exc
        except openai.APIError as exc:
            raise ModelError("The model service returned an API error") from exc

        try:
            choice = response.choices[0]
            if choice.finish_reason == "length":
                raise ModelError("The model output was truncated; check reasoning settings or increase max_tokens")
            message = choice.message
            content = message.content
            if not isinstance(content, str):
                raise TypeError("content must be a string")
            return self._result(content, self._reasoning(message))
        except (AttributeError, IndexError, TypeError) as exc:
            raise ModelError("The model service returned an invalid response structure") from exc

    async def _read_stream(self, stream, request_id: str, on_delta=None) -> ChatResult:
        content_parts = []
        reasoning_parts = []
        finish_reason = None
        # Closing the SDK stream also releases its connection on cancellation.
        async with stream:
            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    content = delta.content
                    if content is not None and not isinstance(content, str):
                        raise TypeError("stream content must be a string")
                    reasoning = self._reasoning(delta)
                    if content:
                        content_parts.append(content)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
                    logger.debug("Model stream [%s]: %s", request_id, json.dumps({
                        "content": content,
                        "reasoning": reasoning,
                        "finish_reason": choice.finish_reason,
                    }, ensure_ascii=False))
                    if on_delta is not None and (content or reasoning):
                        await on_delta(content, reasoning)
            except (AttributeError, IndexError, TypeError) as exc:
                raise ModelError("The model service returned an invalid stream structure") from exc
        if finish_reason == "length":
            raise ModelError("The model output was truncated; check reasoning settings or increase max_tokens")
        if finish_reason is None:
            raise ModelError("The model stream ended before completion")
        return self._result("".join(content_parts), "".join(reasoning_parts) or None)

    @staticmethod
    def _reasoning(message) -> str | None:
        # Preserve whitespace in deltas so concatenation reproduces the output.
        return next((
            value for key in ("reasoning", "reasoning_content")
            if isinstance(value := getattr(message, key, None), str) and value
        ), None)

    @staticmethod
    def _result(content: str, reasoning: str | None) -> ChatResult:
        # Some compatible servers put reasoning before the JSON in think tags.
        tagged = re.fullmatch(r"\s*<think>(.*?)</think>\s*(.*)", content, re.DOTALL)
        if tagged:
            reasoning = reasoning or tagged.group(1).strip() or None
            content = tagged.group(2)
        return ChatResult(content=content, reasoning=reasoning)


def parse_result(content: str) -> dict:
    if not isinstance(content, str):
        raise ModelError("The model output is not text")
    content = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL | re.IGNORECASE)
    if fence:
        content = fence.group(1).strip()
    try:
        def reject_constant(value):
            raise ValueError(f"Invalid JSON constant: {value}")

        def parse_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("JSON number is outside the supported range")
            return number

        data = json.loads(content, parse_constant=reject_constant, parse_float=parse_float)
    except (ValueError, TypeError) as exc:
        raise ModelError("The model output is not valid JSON; adjust the prompt or change the model") from exc
    if not isinstance(data, dict):
        raise ModelError("The model output must be a JSON object; specify fields in the prompt")
    return data
