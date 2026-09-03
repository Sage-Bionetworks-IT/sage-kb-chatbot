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

By default the bot is available to everyone in the workspace. Access can be narrowed with two optional Slack User Group lists:

- An **allow list** — when set, only members of those groups may use the bot.
- A **deny list** — members of those groups are always blocked, even if they're in an allowed group.

If you're not authorized, the bot replies with an ephemeral message saying it's only available to Sage staff; contact your workspace admin to be added to an allowed group. Admins can configure the lists via `slack_authorized_usergroups` and `slack_excluded_usergroups` — see [CONTRIBUTING.md](CONTRIBUTING.md#authorization-user-groups).

### What to expect while it works

The bot gives live feedback so you know it's working on your question:

1. It adds a 👀 reaction to your message the moment it's received.
2. It posts a **⏳ Thinking...** placeholder reply, which updates to show what it's searching (for example, **⏳ Searching Confluence and Jira...**) as each source is queried.
3. When the answer is ready, that placeholder is edited in place to become the final answer — so the thread stays tidy.
4. The 👀 reaction on your message is swapped for a ✅ once the answer is posted.

This feedback is best-effort: if Slack rejects a reaction or placeholder update, the bot still delivers your answer (posting a fresh reply if needed). Slash commands skip the reactions since there's no message to react to, but they still show the thinking placeholder.

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
