"""Answer formatting utilities for Slack mrkdwn output."""

from __future__ import annotations

from slack_agent_router.models import AgentResponse, ToolOutput


def format_answer(response: AgentResponse, elapsed_seconds: float) -> str:
    """Format an AgentResponse as Slack mrkdwn.

    Produces a message with the answer text, numbered source
    links with system labels, and a latency footer.
    """
    parts = [f"*Here's what I found:*\n\n{response.answer}"]

    if response.source_urls:
        lines = ["*Sources:*"]
        for i, url in enumerate(response.source_urls, 1):
            lines.append(f"{i}. <{url}>")
        parts.append("\n".join(lines))

    n = len(response.source_urls)
    label = "source" if n == 1 else "sources"
    footer = f"_Synthesized from {n} {label} in {elapsed_seconds:.1f}s_"
    parts.append(footer)

    return "\n\n".join(parts)


def format_fallback_answer(
    tool_outputs: list[ToolOutput],
) -> str:
    """Format a fallback response from raw tool outputs.

    Used when the Bedrock Agent fails after successful backend
    calls. Concatenates content and sources from each output.
    """
    prefix = "I had trouble synthesizing a complete answer, but here's what I found from each source:"

    if not tool_outputs:
        return prefix

    sections = []
    for output in tool_outputs:
        section_parts = [output.content]
        for source in output.sources:
            url = source.get("url", "")
            title = source.get("title", "Link")
            system = source.get("system", "")
            label = f"<{url}|{title}> ({system})" if system else f"<{url}|{title}>"
            section_parts.append(label)
        sections.append("\n".join(section_parts))

    return f"{prefix}\n\n" + "\n\n".join(sections)
