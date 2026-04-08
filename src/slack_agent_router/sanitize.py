"""Input sanitization utilities.

strip_slack_formatting: removes Slack mrkdwn markup from user input
sanitize_backend_response: neutralizes dangerous content from backends
"""

from __future__ import annotations

import re


def strip_slack_formatting(text: str) -> str:
    """Remove Slack mrkdwn markup and return plain text.

    Strips: bold, italic, strikethrough, inline code, code blocks,
    links, user/channel mentions, and emoji shortcodes.
    """
    # Code blocks (``` ... ```) — extract inner content
    text = re.sub(r"```\n?(.*?)\n?```", r"\1", text, flags=re.DOTALL)

    # Inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Links: <url|label> → label
    text = re.sub(r"<[^|>]+\|([^>]+)>", r"\1", text)

    # User mentions: <@U12345678>
    text = re.sub(r"<@[^>]+>", "", text)

    # Channel mentions: <#C12345678>
    text = re.sub(r"<#[^>]+>", "", text)

    # Special mentions: <!channel>, <!here>, <!everyone>
    text = re.sub(r"<![^>]+>", "", text)

    # Bold: *text*
    text = re.sub(r"\*([^*]+)\*", r"\1", text)

    # Italic: _text_
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)

    # Strikethrough: ~text~
    text = re.sub(r"~([^~]+)~", r"\1", text)

    # Emoji shortcodes: :name:
    text = re.sub(r":[a-z0-9_+-]+:", "", text)

    return text.strip()


def sanitize_backend_response(content: str) -> str:
    """Neutralize dangerous content from backend responses.

    Removes Slack special mentions, user mentions, and other
    potentially dangerous mrkdwn that could trigger notifications
    or inject formatting when posted to Slack.
    """
    # Special mentions that trigger notifications
    text = re.sub(r"<!channel>", "@channel", content)
    text = re.sub(r"<!here>", "@here", text)
    text = re.sub(r"<!everyone>", "@everyone", text)

    # User mentions: <@U12345678> → @user
    text = re.sub(r"<@([^>]+)>", r"@\1", text)

    # Any remaining angle-bracket mentions
    text = re.sub(r"<!([\w]+)>", r"@\1", text)

    return text
