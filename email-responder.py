#!/usr/bin/env python3
"""
SR HyperScale Response Agent — NEB Server
Replaces Lindy agent. Powered by OpenClaw + Claude.

Flow:
1. Gmail trigger (unread inbox, filtered)
2. Rate limit check (max 7 per 12 hours)
3. CRM/client check (Notion + known domains)
4. Cold pitch classification (Claude Sonnet)
5. AI-crafted response with HyperScale pitch (Claude Opus)
6. Send reply in thread
7. Notify #neb-sales
8. Detect interest → Log + Linear ticket

Two-message strategy:
  1st reply: Natural product acknowledgment + casual question about their process
  2nd reply: Decline + pivot to {{YOUR_PRODUCT}}
"""

import json
import os
import re
import sys
import time
import base64
import random
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── Config ──

MATON_KEY = os.environ.get("MATON_API_KEY", "")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
LINEAR_KEY = os.environ.get("LINEAR_API_KEY", "")
SLACK_CHANNEL = "C06P8C6R3H8"  # #neb-sales
STATE_FILE = os.path.expanduser("~/.openclaw/workspace/email-responder-state.json")
CRM_DB_ID = "31b5b322-45b0-8052-adce-ffe58a23f1e1"
LOG_FILE = os.path.expanduser("~/logs/email-responder.log")

FROM_EMAIL = "{{YOUR_EMAIL}}"
FROM_NAME = "{{YOUR_NAME}}"
INTERNAL_DOMAINS = ["aiadvantageagency.co", "teamnebula.ai"]

# Shared writing style injected into every prompt
WRITING_STYLE = """
WRITING STYLE (critical, must sound human):
- Casual founder voice. Think Sahil Lavingia or Pieter Levels energy.
- Short sentences. Sometimes incomplete.
- No corporate speak. No "I hope this email finds you well." No "Thank you for reaching out."
- No AI patterns whatsoever. No "I'd be happy to" or "That's a great question."
- NEVER use em dashes (— or --). These are a dead giveaway for AI writing. Use periods or commas instead.
- NEVER use semicolons. Break into two sentences.
- Avoid starting consecutive sentences with the same word.
- Don't over-qualify things ("actually", "honestly", "to be frank" more than once).
- Slightly impatient but genuinely interested when something catches your eye.
- Use lowercase for casual emphasis, not caps.
- OK to start sentences with "hey" or "so" or "yeah".
- 2-4 short paragraphs max. No bullet points in emails.
- Vary sentence length. Mix short punchy lines with slightly longer ones.
- Use contractions naturally (we're, that's, don't, it's).
- Correct grammar, but conversational. Like texting a business contact you respect.

ABSOLUTE RULES:
- NEVER sound like an AI or auto-responder
- NEVER use em dashes (— or --)
- NEVER use semicolons
- NEVER use "I'd be happy to", "I'd love to", "Great question", "Thanks for reaching out"
- NEVER use "leverage", "synergy", "streamline", "cutting-edge", "game-changer"
- Keep it under 120 words
"""

# No rate limit — respond to all verified cold pitches
MAX_RESPONSES_PER_12H = 9999

# Resources
HYPERSCALE_URL = "https://teamnebula.ai/hyperscale"
BOOKING_LINK = "https://calendly.com/d/cx9h-2kx-6n7/discover-team-nebula"

# Internal/automated senders — never respond
NEVER_RESPOND_PATTERNS = [
    # Internal team
    "aj@", "shawn@", "abraham@", "support@aiadvantageagency",
    "ian@aiadvantageagency",
    # Automated/transactional
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "billing@", "receipts@", "invoices@", "payments@",
    "support@", "hello@", "team@", "info@",
    "notifications@", "notify@", "alerts@", "digest@",
    "updates@", "newsletter",
    # Known platforms
    "@linkedin.com", "@stripe.com", "@paypal.com", "@gamma.app",
    "@google.com", "@slack.com", "@zoom.us", "@calendly.com",
    "@notion.so", "@vercel.com", "@firebase.google.com",
    "@github.com", "@substack.com", "@mailchimp.com",
    "@mail.beehiiv.com", "@costalerts.amazonaws.com",
    "@redditmail.com", "fireflies.ai", "taskade.com",
]

# Subject/body keywords to skip
SKIP_SUBJECT_KEYWORDS = [
    "unsubscribe", "receipt", "invoice", "payment",
    "notification", "reminder", "password reset",
    "verify your email", "confirm your",
]

# Known client domains
CLIENT_DOMAINS = [
    "trophycommercial.com", "rs21.io", "rivus.mx",
    "newcalgon.com", "aicollective.com",
]

MAX_EMAILS_PER_RUN = 50


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass


# ── State Management ──

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {
            "processed_ids": [],
            "response_log": [],      # [{timestamp, email, thread_id, message_num}]
            "thread_state": {},       # thread_id -> {message_num, last_category, sender_email}
            "last_check": None,
        }


def save_state(state):
    state["processed_ids"] = state["processed_ids"][-500:]
    # Keep response_log for last 7 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    state["response_log"] = [r for r in state["response_log"] if r.get("timestamp", "") > cutoff]
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def count_recent_responses(state):
    """Count responses in the last 12 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    return sum(1 for r in state.get("response_log", []) if r.get("timestamp", "") > cutoff)


# ── HTTP Helpers ──

def http_get(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.getcode(), json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def http_post(url, headers, data, timeout=15):
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.getcode(), json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


# ── CRM / Client Check ──

def get_crm_contacts():
    """Fetch all contacts from Notion CRM — emails and domains."""
    emails = set()
    domains = set()
    try:
        data = json.dumps({"page_size": 100}).encode()
        req = urllib.request.Request(
            f"https://gateway.maton.ai/notion/v1/databases/{CRM_DB_ID}/query",
            data=data, method="POST"
        )
        req.add_header("Authorization", f"Bearer {MATON_KEY}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Notion-Version", "2022-06-28")
        resp = json.load(urllib.request.urlopen(req, timeout=15))
        for page in resp.get("results", []):
            email = page.get("properties", {}).get("Email", {}).get("email", "")
            if email:
                emails.add(email.lower())
                domains.add(email.split("@")[-1].lower())
    except Exception as e:
        log(f"WARNING: CRM fetch failed: {e}")
    return emails, domains


def is_blocked_sender(email, subject, crm_emails, crm_domains):
    """Multi-layer filtration. Returns (blocked: bool, reason: str)."""
    email_lower = email.lower()

    # Internal
    if any(email_lower.endswith(f"@{d}") for d in INTERNAL_DOMAINS):
        return True, "internal"

    # Never-respond patterns
    if any(pat in email_lower for pat in NEVER_RESPOND_PATTERNS):
        return True, "automated/platform"

    # Subject keywords
    subj_lower = subject.lower()
    if any(kw in subj_lower for kw in SKIP_SUBJECT_KEYWORDS):
        return True, "transactional subject"

    # CRM exact email
    if email_lower in crm_emails:
        return True, "CRM contact"

    # CRM domain
    domain = email_lower.split("@")[-1]
    if domain in crm_domains:
        return True, "CRM domain"

    # Client domains
    if domain in [d.lower() for d in CLIENT_DOMAINS]:
        return True, "client domain"

    return False, ""


# ── Gmail API ──

def get_recent_emails(max_results=20):
    url = (
        f"https://gateway.maton.ai/google-mail/gmail/v1/users/me/messages"
        f"?q=is:unread+in:inbox+-category:promotions+-category:social+-category:updates"
        f"&maxResults={max_results}"
    )
    code, data = http_get(url, {"Authorization": f"Bearer {MATON_KEY}"})
    if code != 200:
        log(f"Failed to fetch emails: HTTP {code}")
        return []
    return data.get("messages", [])


def get_email_detail(msg_id):
    url = f"https://gateway.maton.ai/google-mail/gmail/v1/users/me/messages/{msg_id}?format=full"
    code, data = http_get(url, {"Authorization": f"Bearer {MATON_KEY}"})
    return data if code == 200 else None


def get_email_body(msg):
    """Extract plain text body from email."""
    payload = msg.get("payload", {})

    def _extract(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            result = _extract(sub)
            if result:
                return result
        return ""

    return _extract(payload)[:3000]  # Cap at 3000 chars


def extract_email_info(msg):
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        headers[h["name"].lower()] = h["value"]

    from_raw = headers.get("from", "")
    email_match = re.search(r'[\w.-]+@[\w.-]+', from_raw)
    sender_email = email_match.group(0) if email_match else from_raw
    name_match = re.match(r'^([^<]+)', from_raw)
    sender_name = name_match.group(1).strip().strip('"') if name_match else sender_email

    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "from_email": sender_email,
        "from_name": sender_name,
        "subject": headers.get("subject", "(no subject)"),
        "snippet": msg.get("snippet", ""),
        "date": headers.get("date", ""),
        "body": get_email_body(msg),
    }


def thread_has_our_reply(thread_id):
    """Check if we already replied in this thread."""
    url = f"https://gateway.maton.ai/google-mail/gmail/v1/users/me/threads/{thread_id}?format=metadata&metadataHeaders=From"
    code, data = http_get(url, {"Authorization": f"Bearer {MATON_KEY}"})
    if code != 200:
        return False
    for tm in data.get("messages", []):
        for h in tm.get("payload", {}).get("headers", []):
            if h["name"].lower() == "from" and any(d in h["value"].lower() for d in INTERNAL_DOMAINS):
                return True
    return False


# ── Claude API ──

def get_oauth_token():
    try:
        with open(os.path.expanduser("~/.openclaw/auth-profiles.json")) as f:
            data = json.load(f)
        profile = data.get("profiles", {}).get("anthropic:oauth", {})
        token = profile.get("access", "")
        expires = profile.get("expires", 0)
        if token and (not expires or time.time() * 1000 < expires):
            return token
    except:
        pass
    return ""


def call_claude(system_prompt, user_prompt, model="claude-sonnet-4-5-20250929", max_tokens=500):
    oauth = get_oauth_token()
    if not oauth:
        log("ERROR: No OAuth token")
        return ""

    data = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}]
    }
    headers = {
        "Authorization": f"Bearer {oauth}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    }

    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers=headers)
        resp = json.load(urllib.request.urlopen(req, timeout=60))
        return resp.get("content", [{}])[0].get("text", "").strip()
    except Exception as e:
        log(f"Claude API error: {e}")
        return ""


# ── Classification ──

def classify_email(info):
    """Classify email. Returns (category, reason)."""
    system = """You are an email classifier for a CEO of an AI consulting company ({{YOUR_COMPANY}} / {{YOUR_PRODUCT}}).
Classify incoming emails with extreme precision. Be very conservative — if ANY doubt, choose AMBIGUOUS.

Categories:
- COLD_PITCH: Someone trying to sell us a service/product. They are pitching THEIR product to us.
- RECRUITMENT: Job offer, hiring pitch, talent acquisition
- INQUIRY: Someone genuinely asking about OUR AI services or wanting to buy from US
- SaaS_PITCH: A SaaS/software company pitching their tool (subtype of cold pitch but we skip these)
- IGNORE: Newsletter, notification, automated, marketing blast, receipt, alert, Substack
- AMBIGUOUS: Anything you're not 100% sure about. When in doubt, ALWAYS choose this.

Critical rules:
- If sender is from a software/SaaS company pitching their platform → SaaS_PITCH
- If it looks like it could be from an existing contact → AMBIGUOUS
- If the email is a reply to something we sent → AMBIGUOUS
- If it mentions any prior relationship or meeting → AMBIGUOUS
- Service emails (hello@, info@, team@) → IGNORE"""

    prompt = f"""Classify this email:

From: {info['from_name']} <{info['from_email']}>
Subject: {info['subject']}
Body preview: {info['body'][:1000]}

Reply with ONLY:
Line 1: Category name
Line 2: One-sentence reason"""

    result = call_claude(system, prompt, max_tokens=100)
    if not result:
        return "AMBIGUOUS", "classification failed"

    lines = result.strip().split("\n")
    category = lines[0].strip().upper().replace(" ", "_")
    reason = lines[1].strip() if len(lines) > 1 else ""

    valid = {"COLD_PITCH", "RECRUITMENT", "INQUIRY", "SAAS_PITCH", "IGNORE", "AMBIGUOUS"}
    if category not in valid:
        category = "AMBIGUOUS"

    return category, reason


# ── AI Response Generation ──

def classify_sender_type(info):
    """Determine if the sender is an agency/consultancy or a direct business."""
    system = """You classify email senders into two categories:
- AGENCY: Marketing agency, sales agency, consulting firm, lead gen agency, SEO agency, PR firm, staffing agency, outsourcing firm, or any company that sells services to other businesses
- DIRECT: A direct business (SaaS, manufacturing, ecommerce, etc.) that is NOT an agency

Look at their email domain, company name, and what they're pitching to determine this."""

    prompt = f"""Classify this sender:
From: {info['from_name']} <{info['from_email']}>
Subject: {info['subject']}
Body preview: {info['body'][:500]}

Reply with ONLY: AGENCY or DIRECT"""

    result = call_claude(system, prompt, max_tokens=10)
    return "AGENCY" if result and "AGENCY" in result.upper() else "DIRECT"


def generate_ai_response(info, message_num, thread_context=""):
    """Generate a human-sounding response using Claude."""

    sign_off = random.choice(["Best,\nShawn", "-Shawn", "Cheers,\nShawn"])

    # Pull KB context for message 2
    kb_context = build_kb_context(info, "cold_pitch") if message_num >= 2 else ""

    if message_num == 1:
        strategy = """FIRST MESSAGE STRATEGY (cold pitch counter):
- Make a specific, genuine observation about their product/service (not generic flattery)
- Show you actually looked at what they're selling
- Ask a casual question about their outreach/sales process or how they're getting results
- Do NOT pitch anything yet
- Do NOT decline yet
- Keep it short (3-5 sentences max)
- Sound like a busy founder who's mildly curious"""

    else:
        # Determine sender type for message 2
        sender_type = classify_sender_type(info)
        log(f"Sender type: {sender_type}")

        if sender_type == "AGENCY":
            strategy = f"""SECOND MESSAGE STRATEGY (agency pivot to HyperScale):
- Casually decline their offer ("timing's not right for us on [their thing]")
- Natural pivot: "but actually, funny timing..." or "on a different note..."
- Pitch {{YOUR_PRODUCT}} as something relevant to THEM or their agency's clients
- Frame it as: "we built this platform that agencies are using to offer AI transformation to their clients"
- Position it as a revenue opportunity for their agency, not a sale
- Include the product link naturally: {HYPERSCALE_URL}
- End with booking link: {BOOKING_LINK}
- Frame the call as a quick chat to see if there's a fit"""
        else:
            strategy = f"""SECOND MESSAGE STRATEGY (direct business - AI solutions pitch):
- Casually decline their offer
- Natural pivot to what WE do
- Pitch: We help businesses use their own data to create and train custom AI agents for almost any pain point — both internal ops and customer-facing
- Range: extremely affordable solutions (thanks to recent AI breakthroughs) all the way up to enterprise-grade systems
- Social proof: "we currently help large enterprises bring in 10m+ per week off the systems we've built for them"
- Keep it casual but confident. Peer-to-peer energy.
- End with: "would love to talk" and include the booking link: {BOOKING_LINK}
- Do NOT include the HyperScale URL for non-agencies"""

    system = f"""You are {{YOUR_NAME}}, CEO of {{YOUR_COMPANY}}. You build AI systems and {{YOUR_PRODUCT}}.
{kb_context}
{WRITING_STYLE}
- Sign off with: {sign_off}

COMPLIMENT PATTERNS (rotate):
1. Notice something specific about their product/tech
2. Business model observation
3. Casual technical respect
4. Market awareness

{strategy}"""

    prompt = f"""Respond to this email:

From: {info['from_name']} <{info['from_email']}>
Subject: {info['subject']}
Body: {info['body'][:2000]}

{f"Previous thread context: {thread_context}" if thread_context else ""}

This is message #{message_num} in our response sequence.
Write the email body only (no subject line, no headers). Plain text, short paragraphs separated by blank lines."""

    result = call_claude(system, prompt, model="claude-sonnet-4-5-20250929", max_tokens=400)
    if not result:
        return None

    # Convert to HTML
    paragraphs = [p.strip() for p in result.split("\n\n") if p.strip()]
    html = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)

    # Linkify URLs
    html = re.sub(
        r'(https?://[^\s<>"]+)',
        r'<a href="\1">\1</a>',
        html
    )

    return html


# ── Notion Knowledge Base Search ──

def get_page_content(page_id, max_blocks=10):
    """Fetch the text content of a Notion page (first N blocks)."""
    try:
        url = f"https://gateway.maton.ai/notion/v1/blocks/{page_id}/children?page_size={max_blocks}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {MATON_KEY}")
        req.add_header("Notion-Version", "2022-06-28")
        resp = json.load(urllib.request.urlopen(req, timeout=15))

        texts = []
        for block in resp.get("results", []):
            btype = block.get("type", "")
            content = block.get(btype, {})
            rich_text = content.get("rich_text", [])
            text = " ".join(t.get("plain_text", "") for t in rich_text).strip()
            if text:
                texts.append(text)

        return " ".join(texts)[:800]
    except:
        return ""


def search_notion_kb(query, max_results=5):
    """Search Notion for relevant content and fetch page bodies."""
    try:
        data = json.dumps({
            "query": query,
            "filter": {"value": "page", "property": "object"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": max_results,
        }).encode()
        req = urllib.request.Request(
            "https://gateway.maton.ai/notion/v1/search",
            data=data, method="POST"
        )
        req.add_header("Authorization", f"Bearer {MATON_KEY}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Notion-Version", "2022-06-28")
        resp = json.load(urllib.request.urlopen(req, timeout=15))

        results = []
        for page in resp.get("results", []):
            props = page.get("properties", {})
            title = ""
            for k, v in props.items():
                if v.get("type") == "title" and v.get("title"):
                    title = " ".join(t.get("plain_text", "") for t in v["title"])
                    break
            if not title:
                continue

            page_id = page.get("id", "")
            url = page.get("url", "")

            # Fetch actual page content
            content = get_page_content(page_id, max_blocks=8)

            # Fallback to property snippets
            if not content:
                for k, v in props.items():
                    if v.get("type") == "rich_text" and v.get("rich_text"):
                        content = " ".join(t.get("plain_text", "") for t in v["rich_text"])[:300]
                        if content:
                            break

            results.append({"title": title, "url": url, "content": content})

        return results
    except Exception as e:
        log(f"Notion KB search error: {e}")
        return []


def build_kb_context(info, context_type="general"):
    """Build a rich knowledge base context block for any response type."""
    queries = []

    if context_type == "cold_pitch":
        # Search for what's relevant to counter-pitch
        queries = [
            "{{YOUR_PRODUCT}} case study results",
            "AI transformation client results revenue",
            "custom AI agents enterprise",
        ]
    elif context_type == "inquiry":
        # Search for what matches their specific ask
        search_terms = (info.get("subject", "") + " " + info.get("snippet", ""))[:150]
        queries = [
            f"HyperScale {search_terms}",
            "case study results transformation",
            "proposals services offerings",
        ]
    else:
        queries = ["{{YOUR_PRODUCT}} services case study"]

    all_results = []
    seen_titles = set()
    for q in queries:
        results = search_notion_kb(q, max_results=3)
        for r in results:
            if r["title"] not in seen_titles:
                seen_titles.add(r["title"])
                all_results.append(r)

    if not all_results:
        return ""

    kb_block = "\n\nKNOWLEDGE BASE (use this intel naturally in your response — never dump it raw):\n"
    for r in all_results[:5]:
        kb_block += f"\n--- {r['title']} ---\n"
        if r.get("content"):
            kb_block += f"{r['content'][:400]}\n"

    return kb_block


def generate_inquiry_response(info):
    """Generate a nurturing response for inbound inquiries using Notion KB context."""

    kb_context = build_kb_context(info, "inquiry")
    sign_off = random.choice(["Best,\nShawn", "-Shawn", "Cheers,\nShawn"])

    system = f"""You are {{YOUR_NAME}}, CEO of {{YOUR_COMPANY}}. Someone reached out asking about your AI services, {{YOUR_PRODUCT}}, or your AI transformation work.

YOUR GOAL: Nurture this lead toward booking a discovery call. Be warm, knowledgeable, genuinely helpful. This person came to YOU.

WHAT WE OFFER:
{{YOUR_PRODUCT}}: AI transformation platform for mid-market companies ({HYPERSCALE_URL})
Custom AI agent builds: from affordable to enterprise-grade
AI transformation partnerships: we embed with teams to transform their operations
Real results: helping enterprises generate 10m+/week from our systems
{kb_context}
{WRITING_STYLE}
- Sign off with: {sign_off}

STRUCTURE:
1. Acknowledge what they're asking about specifically
2. Brief, relevant context on how we solve that exact problem (use KB if relevant)
3. Social proof if it fits naturally
4. Clear CTA: "let's hop on a quick call" with booking link: {BOOKING_LINK}"""

    prompt = f"""Respond to this inbound inquiry:

From: {info['from_name']} <{info['from_email']}>
Subject: {info['subject']}
Body: {info['body'][:2000]}

Write the email body only. Plain text, short paragraphs separated by blank lines."""

    result = call_claude(system, prompt, model="claude-sonnet-4-5-20250929", max_tokens=400)
    if not result:
        return None

    paragraphs = [p.strip() for p in result.split("\n\n") if p.strip()]
    html = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
    html = re.sub(r'(https?://[^\s<>"]+)', r'<a href="\1">\1</a>', html)
    return html


def generate_thread_response(info, action, thread_state_entry):
    """Generate a response based on the detected action. Handles all thread types."""
    kb_context = build_kb_context(info, thread_state_entry.get("type", "cold_pitch"))
    sign_off = random.choice(["Best,\nShawn", "-Shawn", "Cheers,\nShawn"])
    msg_count = thread_state_entry.get("message_num", 1)
    thread_type = thread_state_entry.get("type", "cold_pitch")

    if action == "BOOK_PUSH":
        strategy = f"""They're warm — time to go for the booking. Be direct but natural.
- Acknowledge whatever they said
- One short line of value/social proof
- Clear ask: "let's hop on a quick call" or "grab a time here"
- Booking link: {BOOKING_LINK}
- Keep it under 80 words. Don't oversell."""

    elif action == "NURTURE":
        if thread_type == "inquiry":
            strategy = f"""They're interested in our services. Keep nurturing without being pushy.
- Answer any specific question they asked
- Add one relevant insight, case study detail, or capability that matches their need
- If this is msg 3+, mention the booking link casually at the end: {BOOKING_LINK}
- Keep building rapport. You're a peer, not a salesman."""
        else:
            strategy = f"""They're engaging with us after our counter-pitch. Keep the conversation alive.
- Respond to what they said specifically
- Share one concrete thing about what we do that's relevant to THEM
- Don't push the call yet unless this is msg 3+
- If msg 3+, casually drop: {BOOKING_LINK}"""

    elif action == "SOFT_EXIT":
        strategy = """They're not interested. Be gracious and leave the door open.
- Brief, classy exit. No guilt-tripping.
- Something like "totally get it" or "no worries at all"
- Leave the door open: "if things ever change, happy to chat"
- Do NOT include booking link. Do NOT pitch anything.
- 2-3 sentences max."""

    elif action == "CELEBRATE":
        strategy = f"""They agreed to a call! Confirm it naturally.
- Be excited but cool about it
- If they didn't already click the link, share it: {BOOKING_LINK}
- "grab whatever time works" energy
- Keep it super short, 2-3 sentences"""

    else:  # WAIT or fallback
        return None

    system = f"""You are {{YOUR_NAME}}, CEO of {{YOUR_COMPANY}}. You build AI systems and {{YOUR_PRODUCT}}.

This is message #{msg_count + 1} you're sending in this thread.
{kb_context}
{WRITING_STYLE}
- Sign off with: {sign_off}

{strategy}"""

    prompt = f"""Respond to this email:

From: {info['from_name']} <{info['from_email']}>
Subject: {info['subject']}
Their latest reply: {info['body'][:1500]}

Write email body only. Short paragraphs separated by blank lines."""

    result = call_claude(system, prompt, model="claude-sonnet-4-5-20250929", max_tokens=300)
    if not result:
        return None

    paragraphs = [p.strip() for p in result.split("\n\n") if p.strip()]
    html = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
    html = re.sub(r'(https?://[^\s<>"]+)', r'<a href="\1">\1</a>', html)
    return html


def generate_inquiry_followup(info):
    """Generate a follow-up for inquiry threads — push harder toward booking."""
    kb_context = build_kb_context(info, "inquiry")
    sign_off = random.choice(["Best,\nShawn", "-Shawn", "Cheers,\nShawn"])

    system = f"""You are {{YOUR_NAME}}, CEO of {{YOUR_COMPANY}}. This person already expressed interest in your AI services and you replied. They've responded but haven't booked a call yet.

YOUR GOAL: Get them to book the discovery call. Be direct but not pushy.
{kb_context}
{WRITING_STYLE}
- Sign off with: {sign_off}

APPROACH:
- Acknowledge their reply specifically
- Address any questions they raised
- Add one piece of social proof or specific capability that matches their interest
- Clear, direct ask to book: {BOOKING_LINK}
- Make it easy: "grab a time that works, even 15 min is fine" """

    prompt = f"""Follow up on this inquiry thread:

From: {info['from_name']} <{info['from_email']}>
Their latest reply: {info['body'][:1500]}

Write email body only. Short paragraphs."""

    result = call_claude(system, prompt, model="claude-sonnet-4-5-20250929", max_tokens=300)
    if not result:
        return None

    paragraphs = [p.strip() for p in result.split("\n\n") if p.strip()]
    html = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
    html = re.sub(r'(https?://[^\s<>"]+)', r'<a href="\1">\1</a>', html)
    return html


# ── Send Email ──

def send_reply(info, html_body):
    subject = info["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    subject = subject.encode("ascii", "replace").decode("ascii")

    raw_email = f"""From: {FROM_NAME} <{FROM_EMAIL}>
To: {info['from_email']}
Subject: {subject}
Content-Type: text/html; charset=utf-8
MIME-Version: 1.0

{html_body}"""

    encoded = base64.urlsafe_b64encode(raw_email.encode()).decode()
    send_data = {"raw": encoded, "threadId": info["thread_id"]}

    code, resp = http_post(
        "https://gateway.maton.ai/google-mail/gmail/v1/users/me/messages/send",
        {"Authorization": f"Bearer {MATON_KEY}", "Content-Type": "application/json"},
        send_data
    )

    if code == 200:
        log(f"SENT reply #{info.get('message_num', '?')} to {info['from_email']}")
        return True
    else:
        log(f"FAILED to send to {info['from_email']}: HTTP {code}")
        return False


# ── Interest Detection ──

def detect_interest(info, thread_state_entry):
    """Analyze reply sentiment and decide next action. Returns (sentiment, action, reasoning)."""
    body = info.get("body", "") or info.get("snippet", "")
    if not body.strip():
        return "NEUTRAL", "CONTINUE", ""

    msg_count = thread_state_entry.get("message_num", 1)
    thread_type = thread_state_entry.get("type", "cold_pitch")

    system = f"""You analyze email replies in a sales conversation. You must determine sentiment AND recommend the next action.

This is message #{msg_count} we've sent in this thread. Thread type: {thread_type}.

SENTIMENT (how they feel):
- BOOKED: They explicitly agreed to book a call, schedule a meeting, or said yes to talking. Must be unambiguous.
- WARM: Genuine interest — asking questions about our services, wanting details, engaging meaningfully
- LUKEWARM: Polite but non-committal. Not shutting us down but not leaning in either.
- COLD: Clearly not interested. Said no, asked to stop, got defensive, or is brushing us off.
- NEUTRAL: Can't tell yet — short reply, ambiguous

ACTION (what we should do next):
- BOOK_PUSH: They're warm enough — make a direct but natural ask to book a call. Include the link.
- NURTURE: Keep the conversation going. Add value, share a relevant insight or case study. Don't push yet.
- SOFT_EXIT: They're cold. Be gracious, leave the door open ("if things change, you know where to find us"), stop.
- WAIT: Don't respond yet. Their reply doesn't need a response (e.g., "thanks", "ok", single emoji).
- CELEBRATE: They booked! Confirm the call, be excited but cool about it.

Rules:
- After 4+ messages from us with no real warmth → SOFT_EXIT (don't be annoying)
- If WARM on message 1-2 → NURTURE first, don't rush to BOOK_PUSH
- If WARM on message 3+ → BOOK_PUSH
- If they ask a specific question → always NURTURE with a real answer
- BOOKED sentiment always pairs with CELEBRATE action
- COLD sentiment always pairs with SOFT_EXIT action
- We get lots of emails daily — don't waste time on dead leads"""

    prompt = f"""Analyze this reply (message #{msg_count + 1} from them):

From: {info['from_name']} <{info['from_email']}>
Body: {body[:1500]}

Reply format (3 lines exactly):
Line 1: SENTIMENT
Line 2: ACTION
Line 3: One-sentence reasoning"""

    result = call_claude(system, prompt, max_tokens=60)
    if not result:
        return "NEUTRAL", "CONTINUE", ""

    lines = result.strip().split("\n")
    sentiment = lines[0].strip().upper() if len(lines) > 0 else "NEUTRAL"
    action = lines[1].strip().upper() if len(lines) > 1 else "CONTINUE"
    reasoning = lines[2].strip() if len(lines) > 2 else ""

    valid_sentiments = {"BOOKED", "WARM", "LUKEWARM", "COLD", "NEUTRAL"}
    valid_actions = {"BOOK_PUSH", "NURTURE", "SOFT_EXIT", "WAIT", "CELEBRATE"}

    if sentiment not in valid_sentiments:
        sentiment = "NEUTRAL"
    if action not in valid_actions:
        action = "NURTURE"

    # Hard rules
    if sentiment == "BOOKED":
        action = "CELEBRATE"
    if sentiment == "COLD":
        action = "SOFT_EXIT"

    return sentiment, action, reasoning


# ── CRM (Notion) ──

def add_to_crm(info):
    """Add a prospect to the Notion CRM only when they've agreed to book a call."""
    try:
        data = {
            "parent": {"database_id": CRM_DB_ID},
            "properties": {
                "Name": {"title": [{"text": {"content": info["from_name"]}}]},
                "Email": {"email": info["from_email"]},
                "Notes": {"rich_text": [{"text": {"content": f"Source: Email responder. Subject: {info['subject'][:100]}. Agreed to book a call."}}]},
                "Deal stage": {"select": {"name": "Discovery"}},
                "Last contact date": {"date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d")}},
            }
        }
        code, resp = http_post(
            "https://gateway.maton.ai/notion/v1/pages",
            {"Authorization": f"Bearer {MATON_KEY}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"},
            data
        )
        if code == 200:
            log(f"CRM: Added {info['from_name']} ({info['from_email']})")
        else:
            log(f"CRM: Failed to add: HTTP {code}")
    except Exception as e:
        log(f"CRM error: {e}")


# ── Linear Ticket ──

def create_linear_ticket(info, interest_details):
    """Create a Linear ticket for hot leads."""
    if not LINEAR_KEY:
        log("No LINEAR_API_KEY, skipping ticket creation")
        return

    mutation = """
    mutation($title: String!, $description: String!, $teamId: String!) {
        issueCreate(input: {
            title: $title,
            description: $description,
            teamId: $teamId,
            stateId: "todo"
        }) {
            success
            issue { id identifier url }
        }
    }"""

    variables = {
        "title": f"Hot Lead: {info['from_name']} ({info['from_email']})",
        "description": (
            f"**Lead:** {info['from_name']} ({info['from_email']})\n"
            f"**Subject:** {info['subject']}\n"
            f"**Interest:** {interest_details}\n"
            f"**Source:** Email responder auto-detected HyperScale interest\n"
            f"**Calendar:** {HYPERSCALE_RESOURCES['calendar']}\n\n"
            f"Follow up manually."
        ),
        "teamId": "d0995d59-4974-4da9-a6f1-3dce4af526c0",  # AAAS (AAA Sales)
    }

    code, resp = http_post(
        "https://api.linear.app/graphql",
        {"Authorization": LINEAR_KEY, "Content-Type": "application/json"},
        {"query": mutation, "variables": variables}
    )

    if code == 200 and resp.get("data", {}).get("issueCreate", {}).get("success"):
        ticket = resp["data"]["issueCreate"]["issue"]
        log(f"LINEAR: Created {ticket.get('identifier')} for {info['from_email']}")
        return ticket
    else:
        log(f"LINEAR: Failed to create ticket: {resp}")
        return None


# ── Slack Notification ──

def generate_slack_summary(info, category, message_num, interest=None):
    """Use Claude to write a concise lead summary for Slack."""
    system = """You write ultra-concise Slack notifications about email leads for a sales channel.
Format: 2-3 short lines max. No fluff. Include: who they are, what they want/sell, and what happened.
Use plain text with Slack mrkdwn (*bold* for names/companies). No emoji in your text (emoji is added separately).
NEVER use em dashes or semicolons. Use periods and commas only."""

    body_preview = (info.get("body", "") or info.get("snippet", ""))[:500]

    prompt = f"""Summarize this email interaction for a sales team Slack channel:

From: {info['from_name']} <{info['from_email']}>
Subject: {info['subject']}
Body preview: {body_preview}
Category: {category}
Message #{message_num} in thread
Outcome: {interest or category}

Write 2-3 lines: who is this person/company, what's the context, what happened."""

    result = call_claude(system, prompt, max_tokens=100)
    return result.strip() if result else ""


def notify_slack(info, category, message_num, interest=None):
    if not SLACK_TOKEN:
        return

    if interest == "BOOKED":
        emoji = "🔥"
        header = "BOOKED CALL"
    elif interest == "INTERESTED":
        emoji = "👀"
        header = "WARM LEAD"
    elif interest == "DECLINED":
        emoji = "❌"
        header = "DECLINED"
    elif category == "INQUIRY":
        emoji = "🟢"
        header = "INBOUND INQUIRY"
    else:
        emoji = "📧"
        header = f"AUTO-REPLY (msg #{message_num})"

    # Get concise AI summary
    summary = generate_slack_summary(info, category, message_num, interest)
    if not summary:
        summary = f"{info['from_name']} <{info['from_email']}> — {info['subject'][:60]}"

    text = f"{emoji} *{header}*\n{summary}"

    payload = json.dumps({"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True, "unfurl_links": False}).encode()
    req = urllib.request.Request("https://slack.com/api/chat.postMessage", data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {SLACK_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=10)
    except:
        pass


# ── Main ──

def main():
    log("=== SR HyperScale Response Agent starting ===")

    if not MATON_KEY:
        log("ERROR: No MATON_API_KEY")
        sys.exit(1)

    state = load_state()
    processed_ids = set(state.get("processed_ids", []))
    thread_state = state.get("thread_state", {})

    # Rate limit check
    recent_count = count_recent_responses(state)
    if recent_count >= MAX_RESPONSES_PER_12H:
        log(f"RATE LIMITED: {recent_count}/{MAX_RESPONSES_PER_12H} responses in last 12h. Stopping.")
        return

    remaining_quota = MAX_RESPONSES_PER_12H - recent_count
    log(f"Rate limit: {recent_count}/{MAX_RESPONSES_PER_12H} used, {remaining_quota} remaining")

    # Fetch CRM contacts
    crm_emails, crm_domains = get_crm_contacts()
    log(f"CRM: {len(crm_emails)} contacts, {len(crm_domains)} domains")

    # Fetch emails
    messages = get_recent_emails(max_results=MAX_EMAILS_PER_RUN)
    log(f"Found {len(messages)} unread emails")

    responses_sent = 0

    for msg_stub in messages:
        if responses_sent >= remaining_quota:
            log(f"Hit rate limit quota for this run")
            break

        msg_id = msg_stub.get("id", "")
        if msg_id in processed_ids:
            continue

        msg = get_email_detail(msg_id)
        if not msg:
            continue

        info = extract_email_info(msg)
        sender = info["from_email"].lower()
        thread_id = info["thread_id"]

        # ── STRICT FILTRATION ──

        blocked, reason = is_blocked_sender(sender, info["subject"], crm_emails, crm_domains)
        if blocked:
            log(f"SKIP ({reason}): {sender}")
            processed_ids.add(msg_id)
            continue

        # Check if this is a reply to one of OUR responses (thread tracking)
        if thread_id in thread_state:
            ts = thread_state[thread_id]
            if ts.get("complete"):
                processed_ids.add(msg_id)
                continue

            # Analyze sentiment and decide action
            sentiment, action, reasoning = detect_interest(info, ts)
            log(f"THREAD REPLY from {sender}: sentiment={sentiment} action={action} | {reasoning}")

            if sentiment == "BOOKED":
                # They agreed to a call — confirm, add to CRM, notify
                html = generate_thread_response(info, "CELEBRATE", ts)
                if html:
                    send_reply(info, html)
                    ts["message_num"] += 1
                add_to_crm(info)
                create_linear_ticket(info, f"BOOKED CALL: {info['snippet'][:200]}")
                notify_slack(info, "BOOKED", ts["message_num"], interest="BOOKED")
                ts["complete"] = True

            elif sentiment == "COLD":
                # Graceful exit
                html = generate_thread_response(info, "SOFT_EXIT", ts)
                if html:
                    send_reply(info, html)
                    ts["message_num"] += 1
                    state["response_log"].append({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "email": sender, "thread_id": thread_id,
                        "message_num": ts["message_num"],
                    })
                    responses_sent += 1
                notify_slack(info, "COLD", ts["message_num"], interest="DECLINED")
                ts["complete"] = True

            elif action == "WAIT":
                log(f"WAIT: No response needed for {sender}")

            elif action in ("NURTURE", "BOOK_PUSH"):
                # Cap at 5 messages to avoid being annoying
                if ts["message_num"] >= 5:
                    log(f"MAX MESSAGES (5) reached for {sender}, soft exiting")
                    html = generate_thread_response(info, "SOFT_EXIT", ts)
                    if html:
                        send_reply(info, html)
                        ts["message_num"] += 1
                        responses_sent += 1
                    ts["complete"] = True
                else:
                    html = generate_thread_response(info, action, ts)
                    if html and send_reply(info, html):
                        ts["message_num"] += 1
                        state["response_log"].append({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "email": sender, "thread_id": thread_id,
                            "message_num": ts["message_num"],
                        })
                        notify_slack(info, sentiment, ts["message_num"])
                        responses_sent += 1

            processed_ids.add(msg_id)
            continue

        # Check if we already replied in this thread (from before agent existed)
        if thread_has_our_reply(thread_id):
            log(f"SKIP (already replied in thread): {info['subject'][:50]}")
            processed_ids.add(msg_id)
            continue

        # ── CLASSIFICATION ──
        category, reason = classify_email(info)
        log(f"CLASSIFIED: {sender} | {category} | {reason}")

        # Only respond to cold pitches (not SaaS, not ambiguous)
        if category == "COLD_PITCH":
            message_num = 1
            info["message_num"] = message_num
            html = generate_ai_response(info, message_num)
            if html:
                success = send_reply(info, html)
                if success:
                    thread_state[thread_id] = {
                        "message_num": 1,
                        "sender_email": sender,
                        "sender_name": info["from_name"],
                        "subject": info["subject"],
                        "complete": False,
                    }
                    state["response_log"].append({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "email": sender,
                        "thread_id": thread_id,
                        "message_num": 1,
                    })
                    notify_slack(info, category, 1)
                    responses_sent += 1

        elif category == "INQUIRY":
            # Genuine inbound interest — nurture toward a discovery call
            log(f"INQUIRY from {sender} — nurturing toward booking")
            info["message_num"] = 1
            html = generate_inquiry_response(info)
            if html:
                success = send_reply(info, html)
                if success:
                    thread_state[thread_id] = {
                        "message_num": 1,
                        "sender_email": sender,
                        "sender_name": info["from_name"],
                        "subject": info["subject"],
                        "type": "inquiry",
                        "complete": False,
                    }
                    state["response_log"].append({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "email": sender,
                        "thread_id": thread_id,
                        "message_num": 1,
                    })
                    notify_slack(info, "INQUIRY", 1)
                    responses_sent += 1

        elif category in ("SAAS_PITCH", "IGNORE", "AMBIGUOUS", "RECRUITMENT"):
            log(f"NO RESPONSE ({category}): {sender}")

        processed_ids.add(msg_id)
        time.sleep(2)

    # Save state
    state["processed_ids"] = list(processed_ids)
    state["thread_state"] = thread_state
    save_state(state)
    log(f"=== Done. Sent {responses_sent} responses (total today: {recent_count + responses_sent}/{MAX_RESPONSES_PER_12H}) ===")


if __name__ == "__main__":
    main()
