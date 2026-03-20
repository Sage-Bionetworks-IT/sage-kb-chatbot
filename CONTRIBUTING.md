# Contributing to Sage KB Chatbot

Thank you for your interest in contributing to the Sage Internal Knowledge Slack Chatbot! This guide covers everything you need to get started.

## Coding Standards

- Follow PEP 8 for Python code
- Use type hints for function signatures
- Keep functions small and focused
- Use meaningful variable and function names
- Add docstrings to public functions and classes

### CDK Conventions

- Use `cdk-iam-floyd` for IAM policy generation
- Do not use CDK context for configuration — use environment variables or stack props
- Constructs should not import resources (e.g., `Vpc.fromLookup()`); stacks handle imports
- Constructs should save incoming props as a private field and create resources in protected methods
- Use `PythonFunction` or `NodejsFunction` for Lambda handlers

### Commit Messages

Use [conventional commits](https://www.conventionalcommits.org/):

```
feat(ingestion): add Confluence connector
fix(rag): handle empty search results gracefully
docs(readme): add architecture diagram
test(database): add migration assertions
chore(deps): bump aws-cdk-lib to 2.x
```

### Security

- Never hardcode secrets, API keys, or passwords
- Use Secrets Manager for all credentials
- Follow least-privilege IAM policies — no `*` resource wildcards in production
- Place all compute in private subnets with VPC endpoints

## How to Contribute

### Adding or Improving Code

1. Check the [Tasks](.kiro/specs/sage-kb-chatbot/tasks.md) for open work items
2. Follow the project structure and coding standards above
3. Write tests for new functionality (`pytest -q` to run)
4. Ensure `cdk synth` succeeds before submitting

### Adding Steering Documents

Steering documents (`.kiro/steering/*.md`) provide guidelines for Kiro IDE:

```markdown
---
title: Your Practice Name
inclusion: always  # or fileMatch, manual
fileMatchPattern: '*.ext'  # if using fileMatch
---

# Your Practice Name

- Clear, actionable guidelines
- Specific examples
- Tool recommendations
```

### Adding Agent Hooks

Agent hooks (`.kiro/hooks/*.kiro.hook`) automate actions on IDE events:

```json
{
  "enabled": true,
  "name": "Descriptive Hook Name",
  "description": "What this hook does",
  "version": "1",
  "when": {
    "type": "fileEdited",
    "patterns": ["**/*.py"]
  },
  "then": {
    "type": "askAgent",
    "prompt": "Instructions for the AI agent"
  }
}
```

### Style Guidelines

- Use clear, concise language in docs and prompts
- Include practical examples with proper syntax highlighting
- Reference official documentation when possible
- Keep related practices together with descriptive file names

## Pull Request Process

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Make your changes and test thoroughly
4. Write clear commit messages following conventional commits
5. Include before/after examples in your PR description
6. Tag relevant maintainers for review

## Review Criteria

- **Accuracy** — information is correct and up-to-date
- **Completeness** — covers the topic comprehensively
- **Clarity** — easy to understand and follow
- **Practicality** — provides actionable guidance
- **Performance** — doesn't negatively impact development speed
- **JSON Validity** — all hook files are valid JSON
- **Markdown Formatting** — steering documents are properly formatted

## Ideas for Contributions

### High Priority

- Connector implementations (Confluence, GitHub, Intranet, PowerDMS)
- RAG orchestrator query pipeline
- OpenSearch index configuration and hybrid search
- Observability and alerting constructs

### Medium Priority

- Additional knowledge source connectors
- Advanced chunking strategies
- Prompt engineering improvements
- Performance optimization

## Community

- Open an issue for questions
- Join discussions for brainstorming
- Check existing issues before creating new ones
- Share your customizations and provide feedback

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
