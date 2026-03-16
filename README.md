# Agentic Email Responder

An AI-powered email response agent that intercepts cold sales pitches, nurtures inbound inquiries, and converts conversations to booked discovery calls. Built on Claude + Gmail + Notion.

## Features

- Strict multi-layer email filtration (CRM, domains, patterns)
- Claude-powered email classification
- Two response paths: cold pitch counter + inbound inquiry nurture
- Sentiment-driven thread management (no fixed message limits)
- Notion as knowledge base for intelligent, context-aware responses
- Auto CRM entry on confirmed bookings
- Linear ticket creation for hot leads
- Slack notifications with AI-generated summaries
- Human-sounding founder voice (no AI patterns)

## Architecture

```
Gmail Inbox
  → Filter (internal, automated, CRM, clients)
  → Classify (Claude Sonnet: cold pitch / inquiry / ignore)
  → Generate Response (Claude + Notion KB context)
  → Send in thread
  → Track sentiment on replies
  → Nurture / Book Push / Soft Exit based on signals
  → CRM + Linear + Slack on conversion
```

## Setup

1. Copy `config.example.json` to `config.json` and fill in your details
2. Set env vars: `MATON_API_KEY`, `SLACK_BOT_TOKEN`, `LINEAR_API_KEY`
3. Set up Anthropic OAuth in `~/.openclaw/auth-profiles.json`
4. Replace template variables in `email-responder.py` with your config values
5. Add to cron: `*/15 * * * * /path/to/run-email-responder.sh`

## Required Services

- Gmail (via Maton gateway)
- Notion (CRM database + knowledge base pages)
- Claude API (Anthropic OAuth)
- Slack (notifications)
- Linear (ticket creation, optional)

## Customization

The agent uses multiple Claude prompts you can customize:
- Classification prompt (what counts as cold pitch vs inquiry)
- Response voice and style
- Sentiment detection thresholds
- Notion KB search queries per context type

## License

MIT
