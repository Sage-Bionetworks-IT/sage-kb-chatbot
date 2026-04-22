---
inclusion: fileMatch
fileMatchPattern: "**/backends/**/*.py,**/main.py,**/orchestrator.py,**/slack_*.py"
---

# Secrets Handling

## Runtime Pattern

- Load all secrets from AWS Secrets Manager at startup in `main.py`
- Pass secrets as constructor arguments to components — never as module-level globals
- Components receive plain strings (tokens, credentials) — they don't know about Secrets Manager

```python
# GOOD: main.py loads, passes to constructor
api_token = load_secret("rovo-api-token")
backend = RovoMCPBackend(api_token=api_token, ...)

# BAD: component loads its own secret
class RovoMCPBackend:
    def __init__(self):
        self.token = boto3.client("secretsmanager").get_secret_value(...)
```

## Validation

- Validate all required secrets are present at startup — fail fast with a clear error
- Never fall back to empty strings or defaults for secrets
- Log which secrets were loaded (by name, not value) at INFO level

## Never Expose

- Never log secret values — log secret names only
- Never include secrets in error messages or exception strings
- Never pass secrets through URL query parameters
- Never store secrets in dataclass fields that might be serialized to JSON

## Testing

- Use placeholder strings in tests (e.g., `"test-token-placeholder"`)
- Never use real secrets in test fixtures
- Mock Secrets Manager calls in integration tests
