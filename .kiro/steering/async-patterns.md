---
inclusion: fileMatch
fileMatchPattern: "**/backends/**/*.py,**/slack_*.py,**/orchestrator.py,**/health.py,**/main.py"
---

# Async Patterns

## Timeouts

Always wrap external calls with `asyncio.wait_for`:
```python
result = await asyncio.wait_for(client.call(), timeout=15.0)
```
- Never rely on the remote service to time out for you
- Catch `asyncio.TimeoutError` explicitly — don't let it bubble as a generic `Exception`
- Use per-call timeouts, not just overall request timeouts

## Cancellation

- When a timeout fires, the awaited coroutine is cancelled — ensure cleanup still runs
- Use `try/finally` for resource cleanup, not just `try/except`
- Context managers (`async with`) handle cleanup automatically — prefer them

## Concurrency

- Use `asyncio.gather(*tasks)` for independent concurrent calls
- Set `return_exceptions=True` when partial failure is acceptable
- Never use `asyncio.gather` for sequential dependencies

## Event Loop Safety

- Never call blocking I/O (`requests`, `time.sleep`, file I/O) in async handlers
- Use `httpx.AsyncClient` or `aiohttp` for HTTP, never `requests`
- If you must call sync code, use `asyncio.to_thread()`
- Never create a new event loop inside an async context

## Connection Lifecycle

- Open connections per-request for short-lived protocols (MCP Streamable HTTP)
- Use connection pools for high-frequency calls (httpx, aiohttp)
- Always close connections in `finally` or via `async with`

## Error Handling in Async

- Catch specific exceptions (`asyncio.TimeoutError`, `ConnectionError`, `PermissionError`)
- Log and return structured error results — don't let exceptions propagate to the event loop unhandled
- Use `BackendResult(success=False, ...)` pattern for recoverable failures

## Testing Async Code

- Use `pytest-asyncio` with `asyncio_mode = "auto"` (already configured)
- Mock async methods with `unittest.mock.AsyncMock`
- Test timeout behavior by making mocks raise `asyncio.TimeoutError`
- Test cancellation by verifying cleanup runs after timeout
