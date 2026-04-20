---
inclusion: fileMatch
fileMatchPattern: "**/backends/**/*.py"
---

# MCP Client Conventions

## Connection Lifecycle

Open a fresh connection per request using context managers:
```python
async with streamablehttp_client(url=url, headers=headers) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(name, args)
```
- Always use `async with` — never manage streams manually
- Don't hold connections open between requests
- The MCP SDK handles cleanup on context exit

## Authentication

- Pass auth headers to `streamablehttp_client(headers=...)` — not to `ClientSession`
- Use Bearer tokens for Atlassian, service account credentials for GCP
- Auth tokens come from constructor args (see `secrets-handling.md`)

## Tool Discovery

- Call `session.list_tools()` to discover available tools at runtime
- Match tools by name pattern (e.g., "search", "rovo") — don't hardcode tool names
- Fall back to the first tool if no pattern matches
- Use `list_tools()` as a lightweight health check (no API quota consumed)

## Error Mapping

Map MCP/transport errors to `BackendResult(success=False)`:

| Exception | Meaning | Error message prefix |
|-----------|---------|---------------------|
| `PermissionError` | Auth failure (401/403) | `"Authentication failed: "` |
| `asyncio.TimeoutError` | Request timed out | `"request timed out"` |
| `ConnectionError` | Server unreachable | `"Connection error: "` |
| `result.isError = True` | Tool execution failed | Extract from `result.content` |

- Never let MCP exceptions propagate to callers — always return `BackendResult`
- Log unexpected exceptions at ERROR level with `exc_info=True`

## Response Parsing

- Use `CallToolResult` and `ListToolsResult` types from `mcp.types` for type hints
- Extract text from `result.content` items where `item.type == "text"`
- Join multiple text items with `"\n\n"`
- Extract URLs from text content using regex — strip trailing punctuation
