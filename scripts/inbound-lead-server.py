#!/usr/bin/env python3
"""Direct HTTP server for inbound-lead webhooks — bypasses Hermes entirely.
Runs on port 8645. OpenClaw POSTs here instead of the Hermes webhook adapter.
Does: Twenty CRM write + kanban insert + email draft.
Returns: JSON with everything.
"""
import asyncio
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
from aiohttp import web

DB_PATH = "/Users/brassfieldventuresllc/.hermes/kanban.db"
_TWENTY_API_URL = os.environ.get("TWENTY_API_URL", "http://localhost:3000")
# Load from auth.json if not in environment
_TWENTY_API_KEY = os.environ.get("TWENTY_API_KEY", "")
if not _TWENTY_API_KEY:
    import pathlib
    _auth_file = pathlib.Path.home() / ".hermes" / "auth.json"
    if _auth_file.exists():
        with open(_auth_file, encoding="utf-8") as f:
            _TWENTY_API_KEY = json.load(f).get("twenty_api_key", "")


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        _TWENTY_API_URL + "/graphql", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + _TWENTY_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Twenty API error {e.code}: {body_text[:500]}")


def rest_post(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _TWENTY_API_URL + path, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + _TWENTY_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Twenty REST error {e.code}: {body_text[:500]}")


def next_id(conn):
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(id, 3) AS INTEGER)) FROM tasks WHERE id LIKE 't_%'"
    ).fetchone()[0]
    return f"t_{(row or 0) + 1}"


def draft_outreach(company, contact_name, source, value):
    parts = contact_name.strip().split(" ", 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""

    if value >= 100000:
        value_blurb = f"and I see you're managing significant freight volume (${value:,.0f}+ annually)"
    elif value >= 50000:
        value_blurb = "and your operation looks like it handles solid freight volume"
    else:
        value_blurb = "and I'd love to learn more about your current logistics setup"

    subject = f"Quick question — logistics ops at {company}"
    body = f"""Hi{' ' + first if first else ''},

I noticed {company} {value_blurb} — wanted to reach out because we've been helping logistics teams at similar companies reduce friction in their freight operations.

Specifically, what we're solving: most freight brokers and 3PLs we work with struggle with real-time visibility, carrier quote turnaround, and getting fast answers when something goes sideways. We've built an AI-powered workflow that handles the back-and-forth — from initial quote requests all the way through to POD retrieval — so your team stays focused on relationships and revenue instead of chasing updates.

Curious if that's relevant to what's keeping you busy right now. If it sounds off-base, no worries — happy to learn more about how you're handling it.

Either way — what's the best way to grab 15 minutes this week?

Best,
Andre Brassfield"""
    return {"subject": subject, "body": body}


async def handle_inbound(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    company = payload.get("company", "Unknown Company")
    contact = payload.get("contact_name", "")
    source = payload.get("source", "")
    value_str = payload.get("value_estimate", "0")
    try:
        value = float(str(value_str).replace(",", "").replace("$", ""))
    except (ValueError, AttributeError):
        value = 0.0

    result = {
        "status": "ok", "company": company, "contact": contact,
        "source": source, "value_estimate": value,
        "kanban_task_id": None, "twenty_company_id": None,
        "twenty_person_id": None, "twenty_opportunity_id": None,
        "outreach_draft": None,
    }

    # Kanban task
    conn = sqlite3.connect(DB_PATH)
    task_id = next_id(conn)
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks(id,title,body,assignee,status,kind,created_at,workspace_kind) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (task_id, f"Lead: {company}", json.dumps(payload, indent=2),
         "ares", "done", "task", now, "scratch"),
    )
    conn.commit()
    conn.close()
    result["kanban_task_id"] = task_id

    # Twenty CRM — company
    try:
        r = gql("mutation M($name: String!) { createCompany(data: {name: $name}) { id name } }",
                {"name": company})
        result["twenty_company_id"] = r.get("data", {}).get("createCompany", {}).get("id")
    except Exception as e:
        result["twenty_company_id"] = f"ERROR: {e}"

    # Twenty CRM — person (REST — matches twenty-bridge.py)
    company_id = result["twenty_company_id"]
    if company_id and contact:
        parts = contact.strip().split(" ", 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
        try:
            r = rest_post("/rest/people", {
                "name": {"firstName": first, "lastName": last},
                "companyId": company_id,
            })
            result["twenty_person_id"] = r.get("data", {}).get("createPerson", {}).get("id")
        except Exception as e:
            result["twenty_person_id"] = f"ERROR: {e}"

    # Twenty CRM — opportunity
    if company_id and value > 0:
        try:
            vars = {
                "name": f"{company} — {source}", "stage": "NEW",
                "amountMicros": int(value * 1_000_000),
                "companyId": company_id,
            }
            person_id = result.get("twenty_person_id")
            if person_id and not str(person_id).startswith("ERROR"):
                vars["personId"] = person_id
            r = gql(
                "mutation M($name: String!, $stage: String!, $amountMicros: Long, "
                "$companyId: UUID!, $personId: UUID) { "
                "createOpportunity(data: {name: $name, stage: $stage, "
                "amount: {amountMicros: $amountMicros, currencyCode: \"USD\"}, "
                "companyId: $companyId, pointOfContactId: $personId}) { id } }",
                vars
            )
            result["twenty_opportunity_id"] = r.get("data", {}).get("createOpportunity", {}).get("id")
        except Exception as e:
            result["twenty_opportunity_id"] = f"ERROR: {e}"

    # Draft outreach
    result["outreach_draft"] = draft_outreach(company, contact, source, value)

    return web.json_response(result, status=200)


async def health(request):
    return web.json_response({"status": "ok", "service": "inbound-lead-handler"})


app = web.Application()
app.router.add_get("/health", health)
app.router.add_post("/webhooks/inbound-lead", handle_inbound)

if __name__ == "__main__":
    port = int(os.environ.get("INBOUND_LEAD_PORT", "8645"))
    web.run_app(app, host="127.0.0.1", port=port, print=lambda _: None)
