"""Data models for the Slack Agent Router.

All models are frozen dataclasses for immutability.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedQuestion:
    """Normalized question from any Slack input method."""

    event_type: str
    user_id: str
    channel_id: str
    thread_ts: str | None
    question: str
    team_id: str
    event_ts: str
    request_id: str


@dataclass(frozen=True)
class BackendResult:
    """Internal result from a backend query."""

    backend_name: str
    success: bool
    answer: str | None
    source_urls: list[str]
    error_message: str | None
    latency_ms: float


@dataclass(frozen=True)
class ToolOutput:
    """Structured output from a backend tool execution."""

    success: bool
    content: str
    sources: list[dict[str, str]]
    error_message: str | None


@dataclass(frozen=True)
class AgentResponse:
    """Response from the Bedrock Agent."""

    answer: str
    source_urls: list[str]
    tool_calls_made: list[str]
    latency_ms: float


@dataclass(frozen=True)
class BackendConfig:
    """Configuration for a single backend."""

    name: str
    enabled: bool
    timeout_seconds: int
    secret_arn: str


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limit thresholds."""

    per_user_per_minute: int = 5
    per_user_per_hour: int = 30
    per_user_per_day: int = 100
    per_user_in_flight: int = 1
    global_per_minute: int = 50


@dataclass(frozen=True)
class QueryAuditRecord:
    """Structured audit record for each question-answer cycle."""

    request_id: str
    user_id: str
    channel_id: str
    question: str
    backends_queried: list[str]
    backends_succeeded: list[str]
    backends_failed: list[str]
    agent_model: str | None
    answer_length: int
    total_latency_ms: float
    backend_latencies_ms: dict[str, float]
    agent_latency_ms: float | None
    rate_limited: bool
    timestamp: str
