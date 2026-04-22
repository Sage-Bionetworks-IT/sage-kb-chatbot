---
inclusion: always
---

# Performance Guidelines

## Async I/O

- Use `async/await` for all external calls (Slack, MCP, Bedrock, Vertex AI)
- Use `asyncio.gather()` for independent concurrent backend calls
- Never call blocking I/O in async handlers — see `async-patterns.md`
- Set per-call timeouts on every external request (`asyncio.wait_for`)

## Timeouts

- Backend queries: 15s per backend (configurable via `BackendConfig`)
- Bedrock Agent orchestration: 30s total for `ask()`
- Health check probes: 500ms per backend (`asyncio.wait_for`)
- Slack ack for slash commands: within 3s

## Memory

- Use bounded in-memory caches (TTL-based for dedup, sliding window for rate limiter)
- Implement cleanup for inactive user keys in the rate limiter
- Don't cache full backend responses — cache metadata only
- In-memory state resets on task restart (acceptable for single-task ECS)

## AWS-Specific

- Right-size ECS Fargate task (0.25 vCPU, 0.5 GB for MVP)
- Monitor Bedrock invocation latency and throttling
- Use VPC endpoints if latency to Secrets Manager or Bedrock is high

## General

- Measure before optimizing — don't guess at bottlenecks
- Set performance budgets (response time targets: median <8s, p95 <15s)
- Monitor performance in production via structured logs and CloudWatch

## Anti-Patterns to Avoid

- Synchronous blocking I/O in async handlers
- Unbounded in-memory caches (use TTL or LRU with `cachetools`)
- Opening a new MCP connection per health check probe at high frequency
- Holding MCP connections open between requests
