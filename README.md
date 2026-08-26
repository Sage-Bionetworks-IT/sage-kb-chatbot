# Sage Internal Knowledge Slack Chatbot

A Slack chatbot that answers Sage Bionetworks employee questions by routing queries through an Amazon Bedrock Agent
to fetch information from internal data sources.

## Architecture

```
Slack (Socket Mode) → Slack Agent App → Bedrock Agent Orchestrator → Rovo MCP Backend → Atlassian (Confluence/Jira)
```

The app uses a RETURN_CONTROL action group pattern: the Bedrock Agent decides when to call tools, returns control to
the application, which executes the tool call against the Rovo MCP server, then sends results back to the agent for
answer synthesis.

Key components:

- **SlackAgentApp** — Slack Socket Mode listener, handles messages and mentions
- **BedrockAgentOrchestrator** — Manages the Bedrock Agent conversation loop with timeout and iteration guards
- **RovoMCPBackend** — Calls Atlassian Rovo MCP to search Confluence pages and Jira issues
- **UserGroupAuthorizer** — Checks Slack User Group membership (sage-all) with a cached member list
- **RateLimiter** — Per-user rate limiting
- **AuditLogger** — Structured logging of all interactions

## Usage

There are three ways to interact with the bot in Slack:

### @mention in a channel

Mention the bot in any channel it's been invited to:

```
@sage-kb-chatbot What is our PTO policy?
```

The bot replies in a thread attached to your message.

### Direct message

Send a DM to the bot — no @mention needed:

```
How do I request access to the VPN?
```

### Slash command

Use the `/sage-ask` slash command from any channel:

```
/sage-ask Where can I find the onboarding checklist?
```

The response is posted in the channel where you ran the command.

### Who can use it

The bot is restricted to members of the **sage-all** Slack User Group. If you're not in that group, the bot will respond with an ephemeral message saying it's only available to Sage staff. Contact your workspace admin to be added to the group.

### What it searches

The bot searches Confluence wiki pages and Jira issues via Atlassian Rovo. It works best for questions about:

- Internal processes and policies (PTO, expense reports, onboarding)
- Project documentation and status
- IT procedures and access requests
- HR topics and benefits
- Anything tracked in Confluence or Jira

### Tips

- Ask specific questions — "What is the travel reimbursement policy?" works better than "travel"
- The bot cites its sources with links at the end of each answer
- If the bot can't find relevant information, it will tell you rather than guessing
- Answers from older sources are flagged so you know to verify them

## Development

For setup, configuration, testing, deployment, and external service setup, see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

See [LICENSE](LICENSE).

[sage-kb-chatbot-infra]: https://github.com/Sage-Bionetworks-IT/sage-kb-chatbot-infra
