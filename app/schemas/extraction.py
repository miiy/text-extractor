"""Schemas for prompt-driven extraction requests and responses."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from app.core.config import MAX_GENERATION_TOKENS

Text = Annotated[str, StringConstraints(min_length=1, max_length=100_000, pattern=r"\S")]
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ReasoningEffort = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    text: Text
    result: dict[str, JsonValue]


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: Prompt
    text: Text
    examples: list[Example] | None = Field(default=None, max_length=20)
    model: ModelName | None = None
    # Forward the caller's output token limit to the compatible model server.
    max_tokens: int | None = Field(
        default=None, ge=1, le=MAX_GENERATION_TOKENS,
        description="Output token limit. Omitted or null is not forwarded; the model server chooses its default.",
    )
    temperature: float = Field(default=0, ge=0, le=2)
    reasoning_effort: ReasoningEffort | None = Field(
        default="none",
        description="Defaults to 'none' to disable thinking on supported models. Set a model-supported effort level to enable it, or null to use the model server default.",
    )
    stream: bool = Field(default=False, description="Return SSE events with model deltas and the completed result.")


class ExtractResponse(BaseModel):
    model: str
    text: str
    data: dict[str, JsonValue]
    reasoning: str | None = None
