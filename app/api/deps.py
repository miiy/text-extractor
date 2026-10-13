"""FastAPI dependencies for shared application services."""

from fastapi import Request

from app.services.extraction import ExtractionService


def get_extraction_service(request: Request) -> ExtractionService:
    return request.app.state.extractor
