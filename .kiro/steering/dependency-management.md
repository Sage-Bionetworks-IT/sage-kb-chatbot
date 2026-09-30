---
inclusion: always
description: Dependency management rules and practices
---

# Dependency Management

## Package Manager

This project uses **uv** as the package manager and virtual environment
manager. Always use `uv` commands instead of `pip`, `poetry`, or other tools.

- Add a dependency: `uv add <package>`
- Add a dev dependency: `uv add --dev <package>`
- Remove a dependency: `uv remove <package>`
- Sync environment from lock file: `uv sync`
- Run a command in the project venv: `uv run <command>`
- Update lock file after manual pyproject.toml edits: `uv lock`

## Running Commands

`uv` owns the project virtual environment. Run **every** Python command
— the application, scripts, tools, tests, one-off snippets — through
`uv run` so it executes inside the project venv. Do not activate a venv
by hand, invoke `python`/`pip` directly, or assume a global interpreter.

- Run the app: `uv run python app.py` (or `uv run <entrypoint>`)
- Run a module: `uv run python -m <module>`
- Run a one-off snippet: `uv run python -c "..."`
- Run any installed tool: `uv run <tool>` (e.g. `uv run pytest`, `uv run cdk synth`)

If a command needs a dependency that isn't installed yet, add it with
`uv add` (or `uv sync`) first — never fall back to `pip install`.

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
