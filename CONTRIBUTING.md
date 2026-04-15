# Contributing

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)

## Setup

```bash
# Clone and set up the project
git clone <repo-url>
cd sage-kb-chatbot
uv sync --all-extras
```

This creates a `.venv`, installs all runtime and dev dependencies,
and installs the project in editable mode.

## Common Commands

```bash
# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov

# Run a specific test file
uv run pytest tests/test_sanitize.py -v

# Add a dependency
uv add <package>

# Add a dev dependency
uv add --optional dev <package>

# Update lock file after editing pyproject.toml
uv lock

# Sync environment after pulling changes
uv sync --all-extras
```

## Pre-commit Hooks

```bash
# Install pre-commit hooks (first time only)
uv run pre-commit install

# Run hooks manually
uv run pre-commit run --all-files
```

## Commit Messages

Use [conventional commits](https://www.conventionalcommits.org/):

```
feat(component): add new feature
fix(component): fix a bug
test(component): add or update tests
chore: maintenance tasks
docs: documentation updates
refactor(component): code restructuring
```
