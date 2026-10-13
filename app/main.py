"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager
import logging

import openai
from fastapi import FastAPI

from app.api.routes import router
from app.core.config import Settings
from app.providers.openai import OpenAIProvider
from app.services.extraction import ExtractionService


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings.from_env()
        app_logger = logging.getLogger("app")
        app_logger.setLevel(logging.DEBUG if config.debug else logging.INFO)
        if not app_logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            app_logger.addHandler(handler)
        async with openai.AsyncOpenAI(
            # The SDK requires a value; unauthenticated servers receive no header.
            api_key=config.api_key or "unused",
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=0,
        ) as client:
            app.state.extractor = ExtractionService(OpenAIProvider(
                client, debug=config.debug, omit_auth=not config.api_key,
            ), config)
            yield

    app = FastAPI(title="Text Extractor", version="0.3.0", lifespan=lifespan)

    app.include_router(router)
    return app


app = create_app()
