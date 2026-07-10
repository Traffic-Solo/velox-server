from apps.server.src.api.events import router as events_router
from apps.server.src.core.config import get_settings
from apps.server.src.core.log import configure_logging
from fastapi import FastAPI

configure_logging(get_settings().log_level)

SERVICE_NAME = "VELOX Server"
SERVICE_VERSION = "0.0.1"

app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)
app.include_router(events_router)


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
