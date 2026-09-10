# Agentic Email Responder

An AI-powered email response agent that intercepts cold sales pitches, nurtures inbound inquiries, and converts conversations to booked discovery calls. Built on Claude + Gmail + Notion.

## Features

- Recipient gate — only answers mail actually addressed to your mailbox
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
  → Recipient gate (To / Cc / Delivered-To must name your mailbox)
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
4. Replace template variables in `email-responder.py` with your config values —
   `{{YOUR_EMAIL}}`, `{{YOUR_NAME}}`, `{{YOUR_DOMAIN}}`,
   `{{YOUR_SLACK_CHANNEL_ID}}`, `{{YOUR_NOTION_CRM_DB_ID}}`
5. Shake it out first: `DRY_RUN=1 ./run-email-responder.sh`
6. Add to cron: `*/15 * * * * /path/to/run-email-responder.sh`

## Dry run

`DRY_RUN=1` composes and logs every reply but delivers nothing, and **writes no
state** — so the same messages stay eligible for a later live run to handle for
real. Use it after any prompt or filter change.

```
DRY RUN: would send reply #1 to someone@example.com (842 chars)
DRY RUN: state not written; these messages stay eligible for a live run
```

## Only answer your own mailbox

The recipient gate requires your address in `To`, `Cc`, or `Delivered-To` before a
message reaches the pipeline. Without it an alias, a forwarded copy, or a list you
merely receive all get answered as if addressed to you.

`Delivered-To` matters more than it looks: mass cold outreach is **BCC'd**, so your
address appears in neither `To` nor `Cc` and only in Gmail's own `Delivered-To`
stamp. A gate that reads `To` alone rejects the exact traffic this agent exists to
answer. Matching is exact on the full address, so a different alias on your own
domain still does not qualify.

Run the tests with `python3 test_recipient_gate.py` (stdlib only, nothing to install).

## Deploying

`run-email-responder.sh` on cron is the simplest option. `deploy/deploy-cloud-run.sh`
is a reference push-driven deployment, and it encodes three things worth knowing
wherever you run this:

- **A deploy default that disables sending is a hazard.** `DRY_RUN` defaults to the
  safe value, so a redeploy that leaves it unset pushes dry-run over a live service
  and stops every send without a word — while the logs keep reporting that each pass
  completed. `assert_no_silent_dry_run` refuses that and makes you say which you mean.
- **Test set-ness the same way you default it.** `${VAR+x}` is true for an empty
  string while `${VAR:-default}` treats empty as unset, so an empty override slips
  past a guard written with the first and is then overwritten by the second.
- **Size the request for the whole pass.** A pass runs for minutes inside the push
  request; the request timeout must exceed it, and more than one instance has to be
  free to answer the redelivery your queue sends after its ack deadline. Make that
  redelivery idempotent.

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
