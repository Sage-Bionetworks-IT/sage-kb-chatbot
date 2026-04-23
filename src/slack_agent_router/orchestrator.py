"""Bedrock Agent Orchestrator — manages the return control loop.

Invokes the Amazon Bedrock Agent with user questions, handles tool
call requests via the return control pattern, dispatches to the
correct backend, and returns synthesized answers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Protocol

from slack_agent_router.models import (
    AgentResponse,
    BackendResult,
    ParsedQuestion,
    ToolOutput,
)

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 5
_TOTAL_TIMEOUT_SECONDS = 30.0

# Action group → backend mapping keys
_ACTION_GROUP_ROVO = "SearchConfluenceJira"
_ACTION_GROUP_VERTEX = "SearchGoogleSites"


class _Backend(Protocol):
    """Minimal backend interface used by the orchestrator."""

    @property
    def name(self) -> str: ...

    async def query(self, question: str) -> BackendResult: ...


def derive_session_id(parsed_question: ParsedQuestion) -> str:
    """Derive a Bedrock Agent session ID from Slack thread context.

    - Thread reply: "{channel_id}:{thread_ts}"
    - Channel mention / DM without thread: "{channel_id}:{event_ts}"
    """
    ts = parsed_question.thread_ts if parsed_question.thread_ts else parsed_question.event_ts
    return f"{parsed_question.channel_id}:{ts}"


class BedrockAgentOrchestrator:
    """Manages interaction with the Amazon Bedrock Agent using return control.

    The orchestrator invokes the Bedrock Agent, handles tool call
    requests by dispatching to the correct backend, sends results
    back, and repeats until a final answer is produced.

    Guardrails:
    - Max 5 return control iterations
    - 30s total timeout for ask()
    - Duplicate tool call detection (same action_group + parameters)
    """

    def __init__(
        self,
        agent_id: str,
        agent_alias_id: str,
        rovo_backend: _Backend,
        vertex_backend: _Backend,
        max_iterations: int = _MAX_ITERATIONS,
        timeout_seconds: float = _TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        self._agent_id = agent_id
        self._agent_alias_id = agent_alias_id
        self._backends: dict[str, _Backend] = {
            _ACTION_GROUP_ROVO: rovo_backend,
            _ACTION_GROUP_VERTEX: vertex_backend,
        }
        self._max_iterations = max_iterations
        self._timeout_seconds = timeout_seconds

    async def ask(self, question: str, session_id: str) -> AgentResponse:
        """Send a question to the Bedrock Agent and handle the return control loop.

        Returns an AgentResponse in all cases — never raises.
        """
        start = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._ask_inner(question, session_id, start),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("ask() timed out after %.1fs", self._timeout_seconds)
            return AgentResponse(
                answer="I'm having trouble processing your question right now. Please try again in a few minutes.",
                source_urls=[],
                tool_calls_made=[],
                latency_ms=_elapsed_ms(start),
            )
        except Exception as exc:
            logger.error("Unexpected error in ask(): %s", exc, exc_info=True)
            return AgentResponse(
                answer="I'm having trouble processing your question right now. Please try again in a few minutes.",
                source_urls=[],
                tool_calls_made=[],
                latency_ms=_elapsed_ms(start),
            )

    async def _ask_inner(self, question: str, session_id: str, start: float) -> AgentResponse:
        """Core return control loop logic."""
        tool_calls_made: list[str] = []
        cached_outputs: dict[str, ToolOutput] = {}  # cache key → ToolOutput
        all_source_urls: list[str] = []
        invocation_results: list[dict] | None = None

        for iteration in range(self._max_iterations + 1):
            try:
                response = await self._invoke_agent(
                    question=question,
                    session_id=session_id,
                    return_control_results=invocation_results,
                )
            except Exception as exc:
                logger.error("Agent invocation failed at iteration %d: %s", iteration, exc)
                return self._build_fallback_response(
                    cached_outputs,
                    tool_calls_made,
                    all_source_urls,
                    start,
                )

            # Check if this is a final answer
            if "output" in response:
                return self._parse_final_response(
                    response,
                    tool_calls_made,
                    all_source_urls,
                    start,
                )

            # Check if this is a return control request
            return_control = response.get("returnControl")
            if not return_control:
                logger.warning("Unexpected response format at iteration %d", iteration)
                return self._build_fallback_response(
                    cached_outputs,
                    tool_calls_made,
                    all_source_urls,
                    start,
                )

            # Guard: don't exceed max iterations for tool execution
            if iteration >= self._max_iterations:
                logger.warning("Max iterations (%d) reached", self._max_iterations)
                return self._build_fallback_response(
                    cached_outputs,
                    tool_calls_made,
                    all_source_urls,
                    start,
                )

            # Execute requested tool calls
            invocation_id = return_control.get("invocationId", "")
            invocation_inputs = return_control.get("invocationInputs", [])
            invocation_results = []

            for tool_input in invocation_inputs:
                func_input = tool_input.get("functionInvocationInput", {})
                action_group = func_input.get("actionGroup", "")
                function_name = func_input.get("function", "")
                raw_params = func_input.get("parameters", [])
                parameters = {p["name"]: p["value"] for p in raw_params}

                cache_key = _make_cache_key(action_group, parameters)

                if cache_key in cached_outputs:
                    logger.info("Duplicate tool call detected: %s — using cached result", action_group)
                    tool_output = cached_outputs[cache_key]
                else:
                    tool_output = await self._execute_tool(action_group, function_name, parameters)
                    cached_outputs[cache_key] = tool_output
                    tool_calls_made.append(action_group)

                    if tool_output.success:
                        for src in tool_output.sources:
                            url = src.get("url", "")
                            if url and url not in all_source_urls:
                                all_source_urls.append(url)

                invocation_results.append(
                    self._build_return_control_result(invocation_id, action_group, function_name, tool_output)
                )

        # Fell through the loop without a final answer
        logger.warning("Return control loop exhausted without final answer")
        return self._build_fallback_response(cached_outputs, tool_calls_made, all_source_urls, start)

    async def _invoke_agent(
        self,
        question: str,
        session_id: str,
        return_control_results: list[dict] | None = None,
    ) -> dict:
        """Invoke the Bedrock Agent via boto3.

        This method is designed to be patched in tests. In production,
        it calls bedrock-agent-runtime InvokeAgent.
        """
        import boto3

        client = boto3.client("bedrock-agent-runtime")

        kwargs: dict[str, Any] = {
            "agentId": self._agent_id,
            "agentAliasId": self._agent_alias_id,
            "sessionId": session_id,
            "inputText": question,
        }

        if return_control_results:
            kwargs["sessionState"] = {
                "returnControlInvocationResults": return_control_results,
            }

        response = client.invoke_agent(**kwargs)

        # Parse the streaming response
        completion = response.get("completion", [])
        result: dict = {}
        for event in completion:
            if "returnControl" in event:
                result["returnControl"] = event["returnControl"]
                break
            if "chunk" in event:
                text = event["chunk"].get("bytes", b"").decode("utf-8")
                result.setdefault("output", {}).setdefault("text", "")
                result["output"]["text"] += text

        return result

    async def _execute_tool(self, action_group: str, function_name: str, parameters: dict) -> ToolOutput:
        """Execute a backend call and convert the result to a ToolOutput."""
        backend = self._backends.get(action_group)
        if backend is None:
            logger.warning("Unknown action group: %s", action_group)
            return ToolOutput(
                success=False,
                content="",
                sources=[],
                error_message=f"Unknown action group: {action_group}",
            )

        query_text = parameters.get("query", parameters.get("question", ""))
        try:
            result = await backend.query(query_text)
        except Exception as exc:
            logger.error("Backend %s failed: %s", action_group, exc, exc_info=True)
            return ToolOutput(
                success=False,
                content="",
                sources=[],
                error_message=f"Backend error: {exc}",
            )

        return _backend_result_to_tool_output(result)

    def _parse_final_response(
        self,
        response: dict,
        tool_calls_made: list[str],
        source_urls: list[str],
        start: float,
    ) -> AgentResponse:
        """Extract the synthesized answer from the agent's final response."""
        text = response.get("output", {}).get("text", "")
        return AgentResponse(
            answer=text,
            source_urls=source_urls,
            tool_calls_made=tool_calls_made,
            latency_ms=_elapsed_ms(start),
        )

    def _build_fallback_response(
        self,
        cached_outputs: dict[str, ToolOutput],
        tool_calls_made: list[str],
        source_urls: list[str],
        start: float,
    ) -> AgentResponse:
        """Build a fallback response from cached tool outputs.

        If tool calls succeeded before the failure, concatenate their
        content. Otherwise return a generic error message.
        """
        successful = [o for o in cached_outputs.values() if o.success]

        if not successful:
            return AgentResponse(
                answer="I'm having trouble processing your question right now. Please try again in a few minutes.",
                source_urls=[],
                tool_calls_made=tool_calls_made,
                latency_ms=_elapsed_ms(start),
            )

        from slack_agent_router.formatter import format_fallback_answer

        fallback_text = format_fallback_answer(successful)
        return AgentResponse(
            answer=fallback_text,
            source_urls=source_urls,
            tool_calls_made=tool_calls_made,
            latency_ms=_elapsed_ms(start),
        )

    @staticmethod
    def _build_return_control_result(
        invocation_id: str,
        action_group: str,
        function_name: str,
        tool_output: ToolOutput,
    ) -> dict:
        """Build the returnControlInvocationResults entry for the agent."""
        body = {
            "success": tool_output.success,
            "content": tool_output.content,
            "sources": tool_output.sources,
        }
        if tool_output.error_message:
            body["error"] = tool_output.error_message

        return {
            "functionResult": {
                "actionGroup": action_group,
                "function": function_name,
                "responseBody": {
                    "TEXT": {"body": json.dumps(body)},
                },
            },
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _backend_result_to_tool_output(result: BackendResult) -> ToolOutput:
    """Convert a BackendResult to a ToolOutput for the Bedrock Agent."""
    if not result.success:
        return ToolOutput(
            success=False,
            content="",
            sources=[],
            error_message=result.error_message or "Backend query failed",
        )

    sources = [{"title": "Source", "url": url, "system": result.backend_name} for url in result.source_urls]

    return ToolOutput(
        success=True,
        content=result.answer or "",
        sources=sources,
        error_message=None,
    )


def _make_cache_key(action_group: str, parameters: dict) -> str:
    """Create a deterministic cache key from action group and parameters."""
    sorted_params = json.dumps(parameters, sort_keys=True)
    return f"{action_group}::{sorted_params}"


def _elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since *start* (monotonic)."""
    return (time.monotonic() - start) * 1000
