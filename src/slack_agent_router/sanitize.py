"""Input sanitization utilities.

strip_slack_formatting: removes Slack mrkdwn markup from user input
sanitize_backend_response: neutralizes dangerous content from backends
"""

from __future__ import annotations

import re


def strip_slack_formatting(text: str) -> str:
    """Remove Slack-specific syntax from user input.

    Strips Slack-specific constructs (link syntax, mentions,
    emoji shortcodes) but preserves standard markdown formatting
    (bold, italic, strikethrough, code) since models handle
    markdown well and the semantic emphasis is useful.
    """
    # Links: <url|label> → label
    text = re.sub(r"<[^|>]+\|([^>]+)>", r"\1", text)

    # Bare URLs: <https://example.com> → https://example.com
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)

    # User mentions: <@U12345678>
    text = re.sub(r"<@[^>]+>", "", text)

    # Channel mentions: <#C12345678>
    text = re.sub(r"<#[^>]+>", "", text)

    # Special mentions: <!channel>, <!here>, <!everyone>
    text = re.sub(r"<![^>]+>", "", text)

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

    # Convert Markdown links [text](url) to Slack mrkdwn <url|text>
    text = _markdown_links_to_slack(text)

    # Convert Markdown formatting to Slack mrkdwn
    text = _markdown_to_slack_formatting(text)

    # Escape remaining angle brackets that aren't valid Slack links.
    # Slack treats <...> as special syntax — unrecognized patterns get
    # stripped/hidden. This preserves literal angle-bracket content.
    text = _escape_stray_angle_brackets(text)

    return text


# Matches Markdown links: [display text](url)
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _markdown_links_to_slack(text: str) -> str:
    """Convert Markdown-style links to Slack mrkdwn format.

    [Display Text](https://example.com) → <https://example.com|Display Text>
    """
    return _MARKDOWN_LINK_PATTERN.sub(r"<\2|\1>", text)


def _markdown_to_slack_formatting(text: str) -> str:
    """Convert Markdown formatting to Slack mrkdwn equivalents.

    - **bold** → *bold* (Slack uses single asterisks for bold)
    - ### Heading → *Heading* (Slack has no headings, use bold)
    """
    # Bold: **text** → *text* (must come before heading conversion)
    text = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", text)

    # Headings: ### text, ## text, # text → *text* (bold as substitute)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    return text


# Matches angle-bracket content that is NOT a valid Slack link/mention.
# Valid Slack patterns: <url>, <url|text>, <@U123>, <#C123>, <!here>
_VALID_SLACK_ANGLE_BRACKET = re.compile(r"<(?:https?://[^>]+|@[^>]+|#[^>]+|![^>]+)>")


def _escape_stray_angle_brackets(text: str) -> str:
    """Escape angle brackets that aren't valid Slack mrkdwn syntax.

    Slack interprets <...> as links/mentions. Content like <search>
    or <Confluence> gets stripped. This replaces stray angle brackets
    with their HTML entities so they render as literal characters.
    """

    def _replace_match(m: re.Match[str]) -> str:
        content = m.group(0)
        if _VALID_SLACK_ANGLE_BRACKET.fullmatch(content):
            return content
        # Escape the angle brackets so Slack shows them literally
        return content.replace("<", "&lt;").replace(">", "&gt;")

    return re.sub(r"<[^>]*>", _replace_match, text)
