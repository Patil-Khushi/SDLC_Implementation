"""FastAPI entrypoint for the Implementation Service.

FastAPI exposes the REST API and delegates to the LangGraph workflow. It contains no agent
logic itself. The exec-sandbox executor (integrations/executor.py) is opened ONCE in the
lifespan and held for the process lifetime — never opened/closed per request (CLAUDE.md rule 4)
— then injected into the graph nodes via the executor provider.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config.settings import get_settings
from app.integrations.executor import set_executor

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the exec-sandbox executor once at startup; close it on shutdown."""
    executor = None
    if settings.sandbox_enabled:
        # Lazy import so the app/tests import cleanly even without the MCP deps present.
        from app.integrations.executor import MCPExecutor

        executor = await MCPExecutor.connect(settings.sandbox_mcp_url, settings.sandbox_mcp_transport)
        set_executor(executor)  # inject into the graph nodes (they read it via get_executor())
        logger.info("exec-sandbox executor connected at %s", settings.sandbox_mcp_url)
    else:
        logger.info("SANDBOX_ENABLED=false — running without an exec-sandbox executor")
    try:
        yield
    finally:
        if executor is not None:
            await executor.aclose()
            set_executor(None)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": settings.app_name}
