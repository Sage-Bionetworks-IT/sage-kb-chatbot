"""Property tests for input sanitization.

Property 16: Slack-specific syntax stripping
Property 17: Backend response content sanitization
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
    names = ["smile", "wave", "thumbsup", "rocket", "fire", "heart"]
    name = draw(st.sampled_from(names))
    return f":{name}:", ""


# -------------------------------------------------------
# Property 16: Slack-specific syntax stripping
# Strips mentions, link syntax, emoji shortcodes.
# Preserves markdown formatting (bold, italic, code, etc.)
# -------------------------------------------------------


class TestSlackFormattingStripping:
    """Property 16: stripping removes Slack syntax, preserves markdown."""

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
        assert not any(f":{name}:" in result for name in ["smile", "wave", "thumbsup", "rocket", "fire", "heart"])

    @given(word=plain_word)
    def test_plain_text_unchanged(self, word):
        result = strip_slack_formatting(word)
        assert result == word

    def test_markdown_bold_preserved(self):
        result = strip_slack_formatting("*important*")
        assert "*important*" == result

    def test_markdown_italic_preserved(self):
        result = strip_slack_formatting("_emphasis_")
        assert "_emphasis_" == result

    def test_markdown_code_preserved(self):
        result = strip_slack_formatting("`code`")
        assert "`code`" == result

    def test_markdown_code_block_preserved(self):
        text = "```\nsome code\n```"
        result = strip_slack_formatting(text)
        assert "```" in result

    def test_markdown_strikethrough_preserved(self):
        result = strip_slack_formatting("~deleted~")
        assert "~deleted~" == result


# -------------------------------------------------------
# Property 17: Backend response content sanitization
# -------------------------------------------------------

dangerous_content = st.one_of(
    st.just("<@U12345678>"),
    st.just("<!channel>"),
    st.just("<!here>"),
    st.just("<!everyone>"),
    st.just("<https://evil.com|Click here>"),
    plain_word.map(lambda w: f"<@U999> said {w}"),
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

    @given(content=st.text(min_size=0, max_size=200))
    def test_sanitize_returns_string(self, content):
        result = sanitize_backend_response(content)
        assert isinstance(result, str)

    @given(content=st.text(min_size=0, max_size=200))
    def test_sanitize_is_idempotent(self, content):
        once = sanitize_backend_response(content)
        twice = sanitize_backend_response(once)
        assert once == twice
