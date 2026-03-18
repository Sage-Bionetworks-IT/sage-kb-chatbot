# Contributing to Kiro Best Practices

Thank you for your interest in contributing! This project aims to provide comprehensive best practices for development teams using Kiro IDE.

## 🎯 How to Contribute

### Adding New Best Practices

1. **Steering Documents** (`.kiro/steering/*.md`)
   - Create focused, actionable guidelines
   - Use clear examples and code snippets
   - Reference official documentation when possible
   - Include both "do" and "don't" examples

2. **Agent Hooks** (`.kiro/hooks/*.kiro.hook`)
   - Follow the established JSON format
   - Include clear, specific prompts
   - Test with real projects before submitting
   - Consider performance impact (disable by default if heavy)

### Improving Existing Practices

1. **Update steering documents** with new patterns or tools
2. **Enhance hook prompts** for better AI responses
3. **Add file patterns** for broader coverage
4. **Improve error handling** in automation

## 📋 Contribution Guidelines

### Steering Documents

```markdown
---
title: Your Practice Name
inclusion: always  # or fileMatch, manual
fileMatchPattern: '*.ext'  # if using fileMatch
---

# Your Practice Name

## Section 1
- Clear, actionable guidelines
- Specific examples
- Tool recommendations

## Section 2
- More guidelines
- Code examples
- Best practices
```

### Agent Hooks

```json
{
  "enabled": true,
  "name": "Descriptive Hook Name",
  "description": "Clear description of what this hook does",
  "version": "1",
  "when": {
    "type": "fileEdited",  // or "manual"
    "patterns": ["**/*.ext"],  // or "button_text" for manual
  },
  "then": {
    "type": "askAgent",
    "prompt": "Clear, specific instructions for the AI agent..."
  }
}
```

## 🧪 Testing Your Contributions

### Before Submitting

1. **Test steering documents** - Verify AI follows the guidelines
2. **Test hooks** - Ensure they trigger correctly and provide value
3. **Check file patterns** - Verify they match intended file types
4. **Performance test** - Ensure hooks don't slow down development

### Testing Process

1. Create a test project with relevant files
2. Copy your changes to `.kiro/` directory
3. Restart Kiro IDE
4. Test automatic hooks by saving files
5. Test manual hooks via the Agent Hooks panel
6. Verify steering documents influence AI responses

## 📝 Pull Request Process

### 1. Preparation
- Fork the repository
- Create a feature branch: `git checkout -b feature/your-feature-name`
- Make your changes
- Test thoroughly

### 2. Documentation
- Update README.md if adding new categories
- Update `.kiro/README.md` for hook-specific changes
- Include examples in your PR description

### 3. Submission
- Write clear commit messages following conventional commits
- Include before/after examples
- Explain the problem your contribution solves
- Tag relevant maintainers for review

## 🎨 Style Guidelines

### Steering Documents
- Use clear, concise language
- Include practical examples
- Reference official documentation
- Use consistent formatting
- Add code blocks with proper syntax highlighting

### Hook Prompts
- Be specific about expected actions
- Include error handling instructions
- Reference relevant best practices
- Use numbered lists for complex tasks
- Include tool-specific flags and options

### File Organization
- Keep related practices together
- Use descriptive file names
- Follow existing naming conventions
- Maintain consistent directory structure

## 🔍 Review Criteria

### Quality Standards
- **Accuracy** - Information is correct and up-to-date
- **Completeness** - Covers the topic comprehensively
- **Clarity** - Easy to understand and follow
- **Practicality** - Provides actionable guidance
- **Performance** - Doesn't negatively impact development speed

### Technical Requirements
- **JSON Validity** - All hook files are valid JSON
- **Markdown Formatting** - Steering documents are properly formatted
- **File Patterns** - Patterns match intended file types
- **Testing** - Changes have been tested in real projects

## 🚀 Ideas for Contributions

### High Priority
- Language-specific best practices (Go, Rust, Java, etc.)
- Framework-specific patterns (Next.js, Vue, Angular, etc.)
- Cloud provider integrations (GCP, Azure)
- Database best practices (PostgreSQL, MongoDB, etc.)
- CI/CD automation patterns

### Medium Priority
- IDE-specific optimizations
- Performance monitoring hooks
- Code review automation
- Documentation generation
- Accessibility improvements

### Nice to Have
- Team-specific templates
- Industry-specific patterns
- Advanced automation workflows
- Integration with external tools
- Custom reporting features

## 🤝 Community

### Getting Help
- Open an issue for questions
- Join discussions for brainstorming
- Check existing issues before creating new ones
- Tag maintainers for urgent matters

### Sharing Ideas
- Use GitHub Discussions for ideas
- Share your customizations
- Provide feedback on existing practices
- Suggest improvements to the contribution process

## 📄 License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

Thank you for helping make development better for everyone! 🎉