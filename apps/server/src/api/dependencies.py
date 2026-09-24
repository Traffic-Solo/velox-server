"""Shared API dependencies."""

from typing import Annotated

from apps.server.src.core.config import get_settings
from fastapi import Header, HTTPException, status


def require_api_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Require a bearer token on every API route when VELOX_API_TOKEN is set."""
    expected_token = get_settings().api_token
    if expected_token is None:
        return
    if authorization != "******":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer token",
        )
