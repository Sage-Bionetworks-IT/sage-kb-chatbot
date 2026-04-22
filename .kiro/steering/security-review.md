---
inclusion: manual
---

# Security Review Skill

Activate this skill to perform a security audit on the codebase or recent changes.

## Audit Process

### 1. Secrets Scan
- Search for hardcoded API keys, passwords, tokens, connection strings
- Check .env files are in .gitignore
- Verify secrets are loaded from `os.environ` or AWS Secrets Manager
- Look for secrets in comments, TODOs, or test files

### 2. OWASP Top 10 Check

See `security.md` for the full OWASP checklist and dangerous patterns table. During review, verify each item against the changed code.

### 3. Dependency Audit
- Run `uv run pip-audit`
- Check for known CVEs in dependencies
- Verify `uv.lock` is committed
- Remove unused dependencies

### 4. Dangerous Code Patterns

See `security.md` for the complete list. During review, flag any matches in changed files.

## Output Format

```
[CRITICAL/HIGH/MEDIUM/LOW] Category — Description
  Location: file:line
  Risk: What could go wrong
  Fix: How to remediate
```

## Emergency Protocol

If CRITICAL vulnerability found:
1. Document it immediately
2. Fix before any other work
3. Rotate exposed secrets
4. Check for similar issues elsewhere
