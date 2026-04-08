"""Property tests for input sanitization (RED).

Property 16: Slack formatting markup stripping
Property 17: Backend response content sanitization

These tests should FAIL until task 1.4 implements the
sanitization functions.
"""

from hypothesis import given
from hypothesis import strategies as st

from slack_agent_router.sanitize import (
    sanitize_backend_response,
    strip_slack_formatting,
)

# --- Strategies ---

plain_word = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        min_codepoint=65,
        max_codepoint=122,
    ),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")


@st.composite
def slack_bold_text(draw):
    """Generate text wrapped in Slack bold markup."""
    word = draw(plain_word)
    return f"*{word}*", word


@st.composite
def slack_italic_text(draw):
    """Generate text wrapped in Slack italic markup."""
    word = draw(plain_word)
    return f"_{word}_", word


@st.composite
def slack_strikethrough_text(draw):
    """Generate text with Slack strikethrough markup."""
    word = draw(plain_word)
    return f"~{word}~", word


@st.composite
def slack_inline_code(draw):
    """Generate text wrapped in Slack inline code markup."""
    word = draw(plain_word)
    return f"`{word}`", word


@st.composite
def slack_link(draw):
    """Generate a Slack-formatted link."""
    label = draw(plain_word)
    url = f"https://example.com/{draw(plain_word)}"
    return f"<{url}|{label}>", label


@st.composite
def slack_user_mention(draw):
    """Generate a Slack user mention."""
    user_id = draw(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "N"),
                min_codepoint=48,
                max_codepoint=90,
            ),
            min_size=9,
            max_size=11,
        )
    )
    return f"<@{user_id}>", ""


@st.composite
def slack_channel_mention(draw):
    """Generate a Slack channel mention."""
    channel_id = draw(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "N"),
                min_codepoint=48,
                max_codepoint=90,
            ),
            min_size=9,
            max_size=11,
        )
    )
    return f"<#{channel_id}>", ""


@st.composite
def slack_emoji(draw):
    """Generate a Slack emoji shortcode."""
    names = [
        "smile",
        "wave",
        "thumbsup",
        "rocket",
        "fire",
        "heart",
    ]
    name = draw(st.sampled_from(names))
    return f":{name}:", ""


# Combined strategy: any single Slack markup element
slack_markup_element = st.one_of(
    slack_bold_text(),
    slack_italic_text(),
    slack_strikethrough_text(),
    slack_inline_code(),
    slack_link(),
    slack_user_mention(),
    slack_channel_mention(),
    slack_emoji(),
)


# -------------------------------------------------------
# Property 16: Slack formatting markup stripping
# -------------------------------------------------------


class TestSlackFormattingStripping:
    """Property 16: stripping removes markup, preserves content."""

    @given(data=slack_bold_text())
    def test_bold_stripped(self, data):
        marked_up, expected = data
        result = strip_slack_formatting(marked_up)
        assert "*" not in result
        assert expected in result

    @given(data=slack_italic_text())
    def test_italic_stripped(self, data):
        marked_up, expected = data
        result = strip_slack_formatting(marked_up)
        # underscores in the content itself are fine,
        # but wrapping underscores should be gone
        assert not result.startswith("_")
        assert not result.endswith("_")
        assert expected in result

    @given(data=slack_strikethrough_text())
    def test_strikethrough_stripped(self, data):
        marked_up, expected = data
        result = strip_slack_formatting(marked_up)
        assert "~" not in result
        assert expected in result

    @given(data=slack_inline_code())
    def test_inline_code_stripped(self, data):
        marked_up, expected = data
        result = strip_slack_formatting(marked_up)
        assert "`" not in result
        assert expected in result

    @given(data=slack_link())
    def test_link_replaced_with_label(self, data):
        marked_up, expected_label = data
        result = strip_slack_formatting(marked_up)
        assert "<" not in result
        assert ">" not in result
        assert expected_label in result

    @given(data=slack_user_mention())
    def test_user_mention_removed(self, data):
        marked_up, _ = data
        result = strip_slack_formatting(marked_up)
        assert "<@" not in result

    @given(data=slack_channel_mention())
    def test_channel_mention_removed(self, data):
        marked_up, _ = data
        result = strip_slack_formatting(marked_up)
        assert "<#" not in result

    @given(data=slack_emoji())
    def test_emoji_shortcode_removed(self, data):
        marked_up, _ = data
        result = strip_slack_formatting(marked_up)
        assert not any(
            f":{name}:" in result
            for name in [
                "smile",
                "wave",
                "thumbsup",
                "rocket",
                "fire",
                "heart",
            ]
        )

    def test_code_block_stripped(self):
        text = "```\nsome code here\n```"
        result = strip_slack_formatting(text)
        assert "```" not in result
        assert "some code here" in result

    @given(word=plain_word)
    def test_plain_text_unchanged(self, word):
        result = strip_slack_formatting(word)
        assert result == word


# -------------------------------------------------------
# Property 17: Backend response content sanitization
# -------------------------------------------------------


# Strategy: text that might contain dangerous Slack mrkdwn
dangerous_content = st.one_of(
    # Slack mrkdwn injection attempts
    st.just("*bold injection*"),
    st.just("_italic injection_"),
    st.just("~strike injection~"),
    st.just("<https://evil.com|Click here>"),
    st.just("<@U12345678>"),
    st.just("<!channel>"),
    st.just("<!here>"),
    st.just("<!everyone>"),
    # Excessive formatting
    st.just("```\ncode block injection\n```"),
    # General text with embedded markup
    plain_word.map(lambda w: f"*{w}* and <https://x.com|link>"),
)


class TestBackendResponseSanitization:
    """Property 17: sanitization neutralizes dangerous content."""

    @given(content=dangerous_content)
    def test_no_slack_special_mentions_after_sanitize(self, content):
        result = sanitize_backend_response(content)
        assert "<!channel>" not in result
        assert "<!here>" not in result
        assert "<!everyone>" not in result

    @given(content=dangerous_content)
    def test_no_raw_user_mentions_after_sanitize(self, content):
        result = sanitize_backend_response(content)
        assert "<@" not in result

    @given(word=plain_word)
    def test_safe_content_preserved(self, word):
        result = sanitize_backend_response(word)
        assert word in result

    @given(
        content=st.text(
            min_size=0,
            max_size=200,
        )
    )
    def test_sanitize_returns_string(self, content):
        result = sanitize_backend_response(content)
        assert isinstance(result, str)

    @given(
        content=st.text(
            min_size=0,
            max_size=200,
        )
    )
    def test_sanitize_is_idempotent(self, content):
        once = sanitize_backend_response(content)
        twice = sanitize_backend_response(once)
        assert once == twice
