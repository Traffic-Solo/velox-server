"""Local Ollama adapter for Calendar agenda intent classification."""

from typing import Literal
from urllib.parse import urlsplit

import httpx
from apps.server.src.integrations.calendar_agenda_query import (
    CalendarAgendaIntentResolutionError,
    CalendarAgendaIntentResolverExecutionError,
    CalendarAgendaQueryValidationError,
)
from pydantic import BaseModel, ConfigDict


class OllamaCalendarAgendaClassification(BaseModel):
    """The only semantic results accepted from the local model."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["tomorrow", "unsupported"]


class _OllamaMessage(BaseModel):
    content: str


class _OllamaChatResponse(BaseModel):
    message: _OllamaMessage


class OllamaCalendarAgendaIntentResolver:
    """Classify Calendar agenda text through a bounded local Ollama client."""

    _SYSTEM_PROMPT = (
        "Classify only Calendar agenda intent. Do not answer the user's question. "
        "Do not perform actions. Do not infer account identity. Do not produce "
        "Calendar or provider data. Return only the structured classification."
    )

    def __init__(
        self,
        client: httpx.Client,
        *,
        base_url: str,
        model: str,
    ) -> None:
        self._client = client
        self._url = self._validate_base_url(base_url)
        self._model = self._validate_model(model)

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1", "localhost", "::1",
        } or parsed.username is not None or parsed.password is not None:
            raise ValueError("Ollama base URL must use a loopback host")
        return base_url.rstrip("/") + "/api/chat"

    @staticmethod
    def _validate_model(model: str) -> str:
        if not model.strip():
            raise ValueError("Ollama model must be configured")
        return model.strip()

    def resolve(self, text: str) -> str:
        """Classify text without exposing local model or transport details."""
        if not text.strip():
            raise CalendarAgendaQueryValidationError(
                "calendar agenda query text is required",
            )
        try:
            response = self._client.post(
                self._url,
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": self._SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "stream": False,
                    "format": OllamaCalendarAgendaClassification.model_json_schema(),
                    "options": {"temperature": 0},
                },
            )
            response.raise_for_status()
            envelope = _OllamaChatResponse.model_validate(response.json())
            classification = OllamaCalendarAgendaClassification.model_validate_json(
                envelope.message.content,
            )
        except Exception as error:
            raise CalendarAgendaIntentResolverExecutionError(
                "calendar agenda query resolution failed",
            ) from error
        if classification.intent == "unsupported":
            raise CalendarAgendaIntentResolutionError(
                "calendar agenda query is unsupported",
            )
        return classification.intent
