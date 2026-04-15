"""Structured JSON logging and audit trail for the Slack Agent Router.

Emits machine-parseable JSON logs for every question-answer cycle,
backend interaction, and operational event. Secrets are scrubbed
from all log output before emission.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict

from slack_agent_router.models import QueryAuditRecord

logger = logging.getLogger("slack_agent_router.audit")

# Patterns that indicate secrets or credentials.  Each tuple entry is
# (compiled regex, replacement placeholder).  Order matters — longer
# prefixes are checked first so that ``xoxb-…`` is caught before a
# hypothetical shorter prefix that shares a common start.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"xoxb-[A-Za-z0-9\-]+"), "[REDACTED_SLACK_BOT_TOKEN]"),
    (re.compile(r"xapp-[A-Za-z0-9\-]+"), "[REDACTED_SLACK_APP_TOKEN]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]+"), "[REDACTED_API_KEY]"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-_.]+"), "[REDACTED_BEARER_TOKEN]"),
)


def _scrub_secrets(text: str) -> str:
    """Replace any recognised secret patterns with redaction placeholders."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _safe_json(data: dict) -> str:
    """Serialize *data* to a JSON string with all secrets scrubbed."""
    raw = json.dumps(data, default=str)
    return _scrub_secrets(raw)


class AuditLogger:
    """Structured logging and audit trail for all bot interactions.

    Every public method emits a single JSON log line via the standard
    ``logging`` module.  The ``request_id`` field is present in every
    entry for correlation.  Secrets are automatically redacted.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("slack_agent_router.audit")

    # --- question lifecycle -------------------------------------------

    def log_question_received(self, request_id: str, user_id: str, question: str) -> None:
        """Log when a question is received (INFO level)."""
        self._logger.info(
            _safe_json(
                {
                    "event": "question_received",
                    "request_id": request_id,
                    "user_id": user_id,
                    "question_length": len(question),
                }
            )
        )

    def log_backend_result(
        self,
        request_id: str,
        backend_name: str,
        success: bool,
        latency_ms: float,
    ) -> None:
        """Log individual backend query result (INFO level)."""
        self._logger.info(
            _safe_json(
                {
                    "event": "backend_result",
                    "request_id": request_id,
                    "backend_name": backend_name,
                    "success": success,
                    "latency_ms": latency_ms,
                }
            )
        )

    def log_agent_result(
        self,
        request_id: str,
        success: bool,
        latency_ms: float,
        iterations: int,
    ) -> None:
        """Log Bedrock Agent orchestration result (INFO level)."""
        self._logger.info(
            _safe_json(
                {
                    "event": "agent_result",
                    "request_id": request_id,
                    "success": success,
                    "latency_ms": latency_ms,
                    "iterations": iterations,
                }
            )
        )

    def log_answer_posted(self, record: QueryAuditRecord) -> None:
        """Log the complete audit record when answer is posted (INFO).

        The question text is scrubbed of secrets before inclusion.
        """
        data = asdict(record)
        # Scrub secrets from the question before logging.
        data["question"] = _scrub_secrets(data.get("question", ""))
        self._logger.info(_safe_json(data))

    def log_rate_limited(self, request_id: str, user_id: str, reason: str) -> None:
        """Log when a request is rate-limited (WARNING level)."""
        self._logger.warning(
            _safe_json(
                {
                    "event": "rate_limited",
                    "request_id": request_id,
                    "user_id": user_id,
                    "reason": _scrub_secrets(reason),
                }
            )
        )

    def log_error(self, request_id: str, component: str, error: Exception) -> None:
        """Log errors with full context (ERROR level).

        The exception message is scrubbed for secrets before logging.
        """
        self._logger.error(
            _safe_json(
                {
                    "event": "error",
                    "request_id": request_id,
                    "component": component,
                    "error_type": type(error).__name__,
                    "error_message": _scrub_secrets(str(error)),
                }
            )
        )

    # --- connection lifecycle -----------------------------------------

    def log_websocket_connected(self) -> None:
        """Log WebSocket connection event (INFO level)."""
        self._logger.info(_safe_json({"event": "websocket_connected"}))

    def log_websocket_disconnected(self, reason: str = "") -> None:
        """Log WebSocket disconnection event (WARNING level)."""
        self._logger.warning(
            _safe_json(
                {
                    "event": "websocket_disconnected",
                    "reason": _scrub_secrets(reason),
                }
            )
        )
