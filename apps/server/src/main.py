from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apps.server.src.api.calendar import router as calendar_router
from apps.server.src.api.events import router as events_router
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import get_container
from apps.server.src.core.log import configure_logging
from apps.server.src.integrations.calendar_agenda_runtime import (
    live_calendar_agenda_command_service,
)
from fastapi import FastAPI

configure_logging(get_settings().log_level)

SERVICE_NAME = "VELOX Server"
SERVICE_VERSION = "0.0.1"

@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Install the opt-in live agenda service only while its client is open."""
    if not get_settings().calendar_agenda_live:
        yield
        return
    container = get_container()
    previous_service = container.calendar_agenda_command_service
    with live_calendar_agenda_command_service() as service:
        container.calendar_agenda_command_service = service
        try:
            yield
        finally:
            container.calendar_agenda_command_service = previous_service


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)
app.include_router(events_router)
app.include_router(calendar_router)


@app.get("/")
def read_root() -> dict[str, str]:
    return {
        "status": "running",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
    }


@app.get("/health")
def read_health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
    }
