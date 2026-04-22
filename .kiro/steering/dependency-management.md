---
inclusion: always
description: Dependency management rules and practices
---

# Dependency Management

## Package Manager

This project uses **uv** as the package manager. Always use `uv` commands instead of `pip`, `poetry`, or other tools.

- Add a dependency: `uv add <package>`
- Add a dev dependency: `uv add --dev <package>`
- Remove a dependency: `uv remove <package>`
- Sync environment from lock file: `uv sync`
- Run a command in the project venv: `uv run <command>`
- Update lock file after manual pyproject.toml edits: `uv lock`

## Adding Dependencies
- Justify each new dependency with clear technical value
- Prefer well-maintained libraries with active communities
- Check license compatibility before adding
- Lock file (`uv.lock`) ensures reproducible builds

## Maintenance
- Update dependencies regularly, review changelogs
- Run security audits (`uv run pip-audit`)
- Remove unused dependencies promptly (`uv remove <package>`)
- Test after every dependency update (`uv run pytest`)

## Version Constraints
- Use compatible release operators (`>=X.Y,<Z`) for libraries in `pyproject.toml`
- Pin exact versions only when necessary for stability
- Document why specific version constraints exist
- Always commit `uv.lock` to version control
