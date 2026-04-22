---
inclusion: manual
---

# E2E Testing Skill

Activate this skill when writing end-to-end or integration tests for the Slack Agent Router.

## Test Structure

### Fixture Pattern
Encapsulate setup in reusable pytest fixtures:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.fixture
def mock_orchestrator():
    orch = AsyncMock()
    orch.ask.return_value = AgentResponse(answer="...", source_urls=[], ...)
    return orch

@pytest.fixture
def mock_slack_client():
    client = AsyncMock()
    client.reactions_add = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "123"})
    client.chat_update = AsyncMock()
    return client
```

## Best Practices

- Use pytest fixtures for test setup/teardown
- Use `pytest.mark.parametrize` for multiple input scenarios
- Test user-observable behavior, not internal implementation
- Keep tests independent — each test starts from a clean state
- Use `moto` for mocking AWS services (Secrets Manager, Bedrock)
- Use `unittest.mock.AsyncMock` for async Slack/MCP/Bedrock calls

## What to Test E2E

- Full query flow: ParsedQuestion → orchestrator.ask() → formatted Slack response
- Backend error scenarios: single timeout, all fail, agent failure with fallback
- Rate limiting + authorization flow: event → dedup → auth → rate limit → response
- Health check endpoint: HTTP requests to /health with connected/disconnected states
- Progressive UX: reaction → placeholder → update → final answer sequence

## What NOT to Test E2E

- Individual utility functions (use unit tests)
- CDK construct synthesis (use CDK assertions)
- Every possible input combination (use parametrize in unit tests)

## Debugging Failed Tests

1. Check pytest output with `-v` and `--tb=long`
2. Look at mock call args to verify correct API calls
3. Run the specific test in isolation: `uv run pytest -k "test_name" -s`
4. Add `breakpoint()` to stop at a specific point
5. Check fixture teardown for leftover state
