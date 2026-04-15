"""Property tests for audit logging (RED).

Property 14: Audit log structure and completeness — for any
             QueryAuditRecord, the emitted log is valid JSON with
             all required fields.
Property 15: No secrets in log output — for any log entry, the
             output does not contain API tokens, secrets, or
             credentials even if present in input data.

These tests should FAIL until task 2.2 implements the AuditLogger.
"""

import json
import logging

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from slack_agent_router.audit_logger import AuditLogger
from slack_agent_router.models import QueryAuditRecord

# --- Strategies ---

request_id = st.uuids().map(str)

user_id = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "N"), min_codepoint=48, max_codepoint=90),
    min_size=9,
    max_size=11,
).map(lambda s: f"U{s}")

channel_id = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "N"), min_codepoint=48, max_codepoint=90),
    min_size=9,
    max_size=11,
).map(lambda s: f"C{s}")

question_text = st.text(min_size=1, max_size=200).filter(lambda s: s.strip() != "")

backend_name = st.sampled_from(["Atlassian Rovo (Confluence/Jira)", "Google Sites (Vertex AI Search)"])

timestamp_str = st.from_regex(r"2025-0[1-9]-[0-2][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]Z", fullmatch=True)

latency_ms = st.floats(min_value=100.0, max_value=30000.0, allow_nan=False, allow_infinity=False)


@st.composite
def query_audit_record(draw):
    """Generate a valid QueryAuditRecord."""
    queried = draw(st.lists(backend_name, min_size=1, max_size=2, unique=True))
    succeeded = draw(st.lists(st.sampled_from(queried), max_size=len(queried), unique=True)) if queried else []
    failed = [b for b in queried if b not in succeeded]
    backend_lats = {b: draw(latency_ms) for b in queried}

    return QueryAuditRecord(
        request_id=draw(request_id),
        user_id=draw(user_id),
        channel_id=draw(channel_id),
        question=draw(question_text),
        backends_queried=queried,
        backends_succeeded=succeeded,
        backends_failed=failed,
        agent_model=draw(st.one_of(st.none(), st.just("anthropic.claude-3-sonnet"))),
        answer_length=draw(st.integers(min_value=0, max_value=5000)),
        total_latency_ms=draw(latency_ms),
        backend_latencies_ms=backend_lats,
        agent_latency_ms=draw(st.one_of(st.none(), latency_ms)),
        rate_limited=draw(st.booleans()),
        timestamp=draw(timestamp_str),
    )


# Known secret patterns used to verify they never leak into logs.
SECRET_PATTERNS = (
    "xoxb-",  # Slack bot token prefix
    "xapp-",  # Slack app token prefix
    "sk-",  # Generic API key prefix
    "AKIA",  # AWS access key prefix
    "Bearer ",  # Auth header prefix
)

secret_value = st.sampled_from(
    [
        "xoxb-1234567890-abcdefghij",
        "xapp-1-A0B1C2D3E4F-9876543210-deadbeef",
        "sk-proj-abc123secret456",
        "AKIAIOSFODNN7EXAMPLE",
        "Bearer eyJhbGciOiJIUzI1NiJ9.secret",
    ]
)


# -------------------------------------------------------
# Property 14: Audit log structure and completeness
# -------------------------------------------------------

REQUIRED_AUDIT_FIELDS = {
    "request_id",
    "user_id",
    "channel_id",
    "question",
    "backends_queried",
    "backends_succeeded",
    "backends_failed",
    "answer_length",
    "total_latency_ms",
    "backend_latencies_ms",
    "timestamp",
}


class TestAuditLogStructure:
    """Property 14: emitted audit log is valid JSON with all required fields."""

    @given(record=query_audit_record())
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_log_is_valid_json(self, record, caplog):
        """The emitted log entry must be parseable as JSON."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_answer_posted(record)

        # At least one log record should contain valid JSON
        json_found = False
        for log_record in caplog.records:
            try:
                json.loads(log_record.message)
                json_found = True
            except (json.JSONDecodeError, TypeError):
                continue
        assert json_found, "No valid JSON found in log output"

    @given(record=query_audit_record())
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_log_contains_all_required_fields(self, record, caplog):
        """The JSON log must include every required audit field."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_answer_posted(record)

        parsed = None
        for log_record in caplog.records:
            try:
                parsed = json.loads(log_record.message)
                break
            except (json.JSONDecodeError, TypeError):
                continue

        assert parsed is not None, "No valid JSON found in log output"
        missing = REQUIRED_AUDIT_FIELDS - set(parsed.keys())
        assert not missing, f"Missing required fields: {missing}"

    @given(record=query_audit_record())
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_log_request_id_matches(self, record, caplog):
        """The request_id in the log must match the record's request_id."""
        logger = AuditLogger()
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            logger.log_answer_posted(record)

        for log_record in caplog.records:
            try:
                parsed = json.loads(log_record.message)
                assert parsed["request_id"] == record.request_id
                return
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        raise AssertionError("No valid JSON with request_id found")

    @given(record=query_audit_record())
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_log_backends_queried_matches(self, record, caplog):
        """The backends_queried list must match the record."""
        logger = AuditLogger()
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            logger.log_answer_posted(record)

        for log_record in caplog.records:
            try:
                parsed = json.loads(log_record.message)
                assert parsed["backends_queried"] == record.backends_queried
                return
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        raise AssertionError("No valid JSON with backends_queried found")

    @given(record=query_audit_record())
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_log_timestamp_matches(self, record, caplog):
        """The timestamp in the log must match the record's timestamp."""
        logger = AuditLogger()
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            logger.log_answer_posted(record)

        for log_record in caplog.records:
            try:
                parsed = json.loads(log_record.message)
                assert parsed["timestamp"] == record.timestamp
                return
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        raise AssertionError("No valid JSON with timestamp found")

    def test_log_question_received_is_json(self, caplog):
        """log_question_received must emit valid JSON."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_question_received("req-001", "U123", "What is PTO?")

        json_found = False
        for log_record in caplog.records:
            try:
                parsed = json.loads(log_record.message)
                assert "request_id" in parsed
                json_found = True
            except (json.JSONDecodeError, TypeError):
                continue
        assert json_found

    def test_log_backend_result_is_json(self, caplog):
        """log_backend_result must emit valid JSON."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_backend_result("req-001", "Rovo", True, 1234.5)

        json_found = False
        for log_record in caplog.records:
            try:
                parsed = json.loads(log_record.message)
                assert "request_id" in parsed
                json_found = True
            except (json.JSONDecodeError, TypeError):
                continue
        assert json_found

    def test_log_error_is_json(self, caplog):
        """log_error must emit valid JSON with component and error info."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_error("req-001", "orchestrator", RuntimeError("timeout"))

        json_found = False
        for log_record in caplog.records:
            try:
                parsed = json.loads(log_record.message)
                assert "request_id" in parsed
                assert "component" in parsed
                json_found = True
            except (json.JSONDecodeError, TypeError):
                continue
        assert json_found

    def test_log_rate_limited_is_json(self, caplog):
        """log_rate_limited must emit valid JSON."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_rate_limited("req-001", "U123", "5/min exceeded")

        json_found = False
        for log_record in caplog.records:
            try:
                parsed = json.loads(log_record.message)
                assert "request_id" in parsed
                assert "reason" in parsed
                json_found = True
            except (json.JSONDecodeError, TypeError):
                continue
        assert json_found


# -------------------------------------------------------
# Property 15: No secrets in log output
# -------------------------------------------------------


class TestNoSecretsInLogs:
    """Property 15: log output never contains secrets or credentials."""

    @given(record=query_audit_record(), secret=secret_value)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_audit_log_excludes_secrets_in_question(self, record, secret, caplog):
        """Secrets embedded in question text must not appear in log output."""
        poisoned = QueryAuditRecord(
            request_id=record.request_id,
            user_id=record.user_id,
            channel_id=record.channel_id,
            question=f"My token is {secret} please help",
            backends_queried=record.backends_queried,
            backends_succeeded=record.backends_succeeded,
            backends_failed=record.backends_failed,
            agent_model=record.agent_model,
            answer_length=record.answer_length,
            total_latency_ms=record.total_latency_ms,
            backend_latencies_ms=record.backend_latencies_ms,
            agent_latency_ms=record.agent_latency_ms,
            rate_limited=record.rate_limited,
            timestamp=record.timestamp,
        )
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_answer_posted(poisoned)

        full_output = " ".join(r.message for r in caplog.records)
        assert secret not in full_output, f"Secret '{secret}' leaked into log output"

    @given(secret=secret_value)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_question_received_excludes_secrets(self, secret, caplog):
        """Secrets in question text must not appear in log_question_received output."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_question_received("req-001", "U123", f"Token: {secret}")

        full_output = " ".join(r.message for r in caplog.records)
        assert secret not in full_output, f"Secret '{secret}' leaked into log output"

    @given(secret=secret_value)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_error_log_excludes_secrets(self, secret, caplog):
        """Secrets in exception messages must not appear in log_error output."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_error("req-001", "backend", RuntimeError(f"Auth failed: {secret}"))

        full_output = " ".join(r.message for r in caplog.records)
        assert secret not in full_output, f"Secret '{secret}' leaked into log output"

    @given(secret=secret_value)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_rate_limited_log_excludes_secrets(self, secret, caplog):
        """Secrets in reason strings must not appear in log_rate_limited output."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_rate_limited("req-001", "U123", f"Limit exceeded {secret}")

        full_output = " ".join(r.message for r in caplog.records)
        assert secret not in full_output, f"Secret '{secret}' leaked into log output"

    def test_no_secret_prefixes_in_any_log_method(self, caplog):
        """No known secret prefix patterns should appear in any log output."""
        logger = AuditLogger()
        with caplog.at_level(logging.DEBUG):
            logger.log_question_received("req-001", "U123", "xoxb-fake-token help me")
            logger.log_backend_result("req-001", "Rovo", True, 500.0)
            logger.log_rate_limited("req-001", "U123", "xapp-1-secret limit")
            logger.log_error("req-001", "auth", RuntimeError("AKIAIOSFODNN7EXAMPLE"))

        full_output = " ".join(r.message for r in caplog.records)
        for prefix in SECRET_PATTERNS:
            assert prefix not in full_output, f"Secret prefix '{prefix}' found in log output"
