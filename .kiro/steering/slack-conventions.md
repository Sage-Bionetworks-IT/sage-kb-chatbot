---
inclusion: fileMatch
fileMatchPattern: "**/slack_*.py"
---

# Slack API Conventions

## Message Types

| Scenario | Method | Visibility |
|----------|--------|------------|
| Final answer | `chat.postMessage` in thread | Everyone in channel |
| Error / rate limit / unauthorized | `chat.postEphemeral` | Only the user |
| Progress update | `chat.update` on placeholder | Everyone in channel |

- Always reply in a thread (`thread_ts` set) — never top-level
- Use ephemeral messages for user-specific feedback that shouldn't clutter the channel

## Progressive UX

Follow this sequence for every accepted question:
1. Add 👀 reaction immediately
2. Post "⏳ Thinking..." placeholder in thread
3. Update placeholder as backends are queried (e.g., "⏳ Searching Confluence and Jira...")
4. Replace placeholder with final answer via `chat.update`
5. Remove 👀, add ✅

- If processing fails at any step, still remove 👀 and post an error message
- Never leave 👀 hanging on a message with no response

## Formatting

- Use Slack mrkdwn for all bot messages (not Markdown — they differ)
- Bold with `*text*`, italic with `_text_`, code with `` `text` ``
- Links: `<https://example.com|Display Text>`
- Source lists: numbered, with system label — `1. <url|Title> (Confluence)`
- Include latency footer: `_Synthesized from N sources in X.Xs_`

## Acknowledgment

- `app_mention` and `message` events: Bolt auto-acks the envelope — no action needed
- Slash commands: handler must call `ack()` explicitly within 3 seconds
- Do all processing after ack, never before

## Deduplication

- Track `event_id` / `envelope_id` in a TTL cache (60s window)
- Deduplicate slash commands on `trigger_id`
- Skip silently on duplicate — no error message, no logging at WARNING+

## Error Messages

Use consistent wording:
- Empty question: `"Try asking me something like: @bot What is our PTO policy?"`
- Unauthorized: `"Sorry, this bot is only available to Sage staff"`
- Rate limited: include which limit was hit (from `RateLimiter.check()` reason string)
- All backends failed: `"I wasn't able to find an answer right now. Please try again in a few minutes."`
- Agent failure (no tool calls): `"I'm having trouble processing your question right now. Please try again in a few minutes."`
