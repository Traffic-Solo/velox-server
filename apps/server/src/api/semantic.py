"""Domain-neutral semantic query HTTP ingress."""

import logging
from typing import Annotated, Any

from apps.server.src.api.dependencies import require_api_token
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.semantic import (
    SemanticInputError,
    SemanticResolverError,
    SemanticRoutingError,
)
from apps.server.src.core.semantic_query import (
    SemanticQueryRequest,
    SemanticRequestError,
    execute_semantic_query,
)
from apps.server.src.workers.executor import WorkerAccountContext
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)
router = APIRouter(tags=["semantic"], dependencies=[Depends(require_api_token)])


class SemanticAccountContextRequest(BaseModel):
    """Explicit caller-owned account fields; never inferred or defaulted."""

    model_config = ConfigDict(extra="forbid")

    principal: str
    account_identifier: str


class SemanticQueryHttpRequest(BaseModel):
    """Free-form query plus the caller-owned context handlers may require."""

    model_config = ConfigDict(extra="forbid")

    text: str
    account_context: SemanticAccountContextRequest
    timezone: str


class SemanticQueryHttpResponse(BaseModel):
    """Canonical intent and the trusted handler's JSON-safe result payload."""

    intent: str
    result: dict[str, Any]


@router.post("/semantic/query")
def semantic_query(
    request: SemanticQueryHttpRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> SemanticQueryHttpResponse:
    """Resolve free-form text, then run only an application-declared route."""
    try:
        result = execute_semantic_query(
            SemanticQueryRequest(
                text=request.text,
                account_context=WorkerAccountContext(
                    principal=request.account_context.principal,
                    account_identifier=request.account_context.account_identifier,
                ),
                timezone=request.timezone,
            ),
            resolver=container.semantic_resolver,
            router=container.semantic_router,
        )
    except SemanticInputError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid semantic query",
        ) from None
    except SemanticRoutingError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unsupported semantic query",
        ) from None
    except SemanticRequestError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid semantic query request",
        ) from None
    except SemanticResolverError:
        logger.error("semantic query resolution failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="semantic query resolution failed",
        ) from None
    except Exception:
        logger.error("semantic query execution failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="semantic query execution failed",
        ) from None
    return SemanticQueryHttpResponse(intent=result.intent, result=dict(result.result))
