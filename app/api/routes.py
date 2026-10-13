"""HTTP routes for text extraction."""

from contextlib import aclosing
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import get_extraction_service
from app.providers.openai import ModelError, ModelTimeout
from app.schemas.extraction import ExtractRequest, ExtractResponse
from app.services.extraction import ExtractionService

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health():
    """Return a liveness response without checking model readiness."""
    return {"status": "ok"}


@router.post(
    "/extract", response_model=ExtractResponse, response_model_exclude_unset=True,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def extract(
    body: ExtractRequest,
    service: Annotated[ExtractionService, Depends(get_extraction_service)],
):
    logger.debug("POST /extract: %s", body.model_dump_json(exclude_unset=True))
    if body.stream:
        async def events():
            async with aclosing(service.stream(body)) as stream:
                async for event in stream:
                    if event["event"] == "ping":
                        yield ": ping\n\n"
                    else:
                        yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
    try:
        return await service.extract(body)
    except ModelTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ModelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
