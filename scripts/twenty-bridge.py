#!/usr/bin/env python3
"""
twenty-bridge.py — Bridge between local CRM (crm.db) and Twenty CRM
=========================================================================
Local crm.db = Andre's control layer (qualification gates, audit log)
Twenty CRM   = live system-of-record (Docker at localhost:3000/3001)

Architecture:
  Reads  → Direct Postgres (192.168.97.2:5432, Docker internal network)
            No API key needed, bypasses GraphQL overhead
  Writes → Twenty REST API (http://localhost:3000)
            Requires TWENTY_API_KEY from Twenty UI → Settings → API Keys

Dependencies:
  psycopg2-binary (pip install psycopg2-binary)
  Runs via hermes venv: ~/.hermes/hermes-agent/venv/bin/python3

Usage:
    python3 twenty-bridge.py status
    python3 twenty-bridge.py list   [--stage Name] [--json]
    python3 twenty-bridge.py lead   <kanban-task-id> [--dry-run]
    python3 twenty-bridge.py advance <kanban-task-id> [--stage Name] [--notes "..."]
    python3 twenty-bridge.py sync   [--dry-run]   # push all crm.db leads → Twenty
    python3 twenty-bridge.py pg 'SELECT ... FROM {schema}.table' [--json]

Environment:
    TWENTY_API_URL   default http://localhost:3000
    TWENTY_API_KEY   access token from Twenty UI (Settings → API Keys)
    TWENTY_DB_HOST   Postgres host (default 192.168.97.2, Docker internal)
    TWENTY_DB_PORT   Postgres port (default 5432)
    TWENTY_DB_USER   Postgres user (default postgres)
    TWENTY_DB_PASS   Postgres password (default postgres)
    TWENTY_DB_NAME   Postgres database (default default)
    TWENTY_WORKSPACE_SCHEMA  workspace schema (auto-detected)
    CRM_DB           default ~/.hermes/crm.db
"""
import sqlite3, json, sys, os, argparse
from datetime import datetime
from typing import Optional

# ── paths ──────────────────────────────────────────────────────────────────
HOME = os.path.expanduser("~")
CRM_DB = os.environ.get("CRM_DB", HOME + "/.hermes/crm.db")
TWENTY_API_URL = os.environ.get("TWENTY_API_URL", "http://localhost:3000")

# ── Postgres direct-access (God Mode) ──────────────────────────────────────
# Twenty Docker network: db container at 192.168.97.2:5432
# Credentials: postgres/postgres/default
# Schema per workspace, e.g. workspace_58zrdfqblghggov40j07gnown
try:
    import psycopg2
    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False

_PG_HOST = os.environ.get("TWENTY_DB_HOST", "192.168.97.2")
_PG_PORT = int(os.environ.get("TWENTY_DB_PORT", "5432"))
_PG_USER = os.environ.get("TWENTY_DB_USER", "postgres")
_PG_PASS = os.environ.get("TWENTY_DB_PASS", "postgres")
_PG_DBNAME = os.environ.get("TWENTY_DB_NAME", "default")
_PG_SCHEMA = os.environ.get("TWENTY_WORKSPACE_SCHEMA", "workspace_58zrdfqblghggov40j07gnown")


class TwentyDB:
    """Direct Postgres read access to Twenty CRM — bypasses API entirely.

    Connect string is derived from Docker internal network (192.168.97.x).
    Use TWENTY_DB_HOST=host.docker.internal to reach from macOS host
    if Docker networking changes.
    """

    def __init__(self,
                 host: str = _PG_HOST, port: int = _PG_PORT,
                 user: str = _PG_USER, password: str = _PG_PASS,
                 dbname: str = _PG_DBNAME, schema: str = _PG_SCHEMA):
        if not _PG_AVAILABLE:
            raise SystemExit("psycopg2 not installed — run: pip install psycopg2-binary")
        self.schema = schema
        self.conn = psycopg2.connect(host=host, port=port, user=user,
                                    password=password, dbname=dbname)
        self.conn.autocommit = True
        self.cur = self.conn.cursor()

    def _q(self, sql: str, params=None):
        """Execute SQL with schema-qualified table names."""
        sql = sql.replace('"{schema}"', f'"{self.schema}"')
        self.cur.execute(sql, params or ())
        return self.cur.fetchall()

    # ── read ops ────────────────────────────────────────────────────────────

    def list_opportunities(self) -> list[dict]:
        """Return all opportunities with company name, amount, stage, close date."""
        rows = self._q(
            'SELECT "id","name","stage","amountAmountMicros","closeDate",'
            '       "companyId","pointOfContactId","ownerId" '
            'FROM "{schema}"."opportunity" WHERE "deletedAt" IS NULL '
            'ORDER BY "createdAt" DESC')
        result = []
        for r in rows:
            amt = r[3]
            result.append({
                "id": r[0], "name": r[1], "stage": r[2],
                "amountMicros": int(amt) if amt else 0,
                "amountUsd": round(float(amt) / 1e6, 2) if amt else 0,
                "closeDate": r[4], "companyId": r[5],
                "personId": r[6], "ownerId": r[7],
            })
        return result

    def get_opportunities_by_stage(self) -> dict[str, list[dict]]:
        opps = self.list_opportunities()
        by_stage = {}
        for o in opps:
            by_stage.setdefault(o["stage"], []).append(o)
        return by_stage

    def get_company(self, company_id: str) -> Optional[dict]:
        rows = self._q(
            'SELECT "id","name","domainNamePrimaryLinkUrl","employees",'
            '       "accountOwnerId","annualRecurringRevenueAmountMicros" '
            'FROM "{schema}"."company" WHERE "id" = %s AND "deletedAt" IS NULL',
            (company_id,))
        if not rows:
            return None
        r = rows[0]
        arr = r[5]
        return {
            "id": r[0], "name": r[1], "domain": r[2], "employees": r[3],
            "ownerId": r[4],
            "arrMicros": int(arr) if arr else 0,
            "arrUsd": round(float(arr) / 1e6, 2) if arr else 0,
        }

    def get_person(self, person_id: str) -> Optional[dict]:
        rows = self._q(
            'SELECT "id","nameFirstName","nameLastName","jobTitle",'
            '       "emailsPrimaryEmail","phonesPrimaryPhoneNumber","companyId" '
            'FROM "{schema}"."person" WHERE "id" = %s AND "deletedAt" IS NULL',
            (person_id,))
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r[0], "firstName": r[1], "lastName": r[2],
            "jobTitle": r[3], "email": r[4], "phone": r[5], "companyId": r[6],
        }

    def list_companies(self) -> list[dict]:
        rows = self._q(
            'SELECT "id","name","domainNamePrimaryLinkUrl","employees",'
            '       "accountOwnerId" '
            'FROM "{schema}"."company" WHERE "deletedAt" IS NULL '
            'ORDER BY "name"')
        return [{"id": r[0], "name": r[1], "domain": r[2],
                 "employees": r[3], "ownerId": r[4]} for r in rows]

    def list_people(self) -> list[dict]:
        rows = self._q(
            'SELECT "id","nameFirstName","nameLastName","jobTitle",'
            '       "emailsPrimaryEmail","companyId" '
            'FROM "{schema}"."person" WHERE "deletedAt" IS NULL '
            'ORDER BY "nameLastName"')
        return [{"id": r[0], "firstName": r[1], "lastName": r[2],
                 "jobTitle": r[3], "email": r[4], "companyId": r[5]} for r in rows]

    def search_companies(self, name: str) -> list[dict]:
        rows = self._q(
            'SELECT "id","name","domainNamePrimaryLinkUrl","employees" '
            'FROM "{schema}"."company" '
            'WHERE "deletedAt" IS NULL AND LOWER("name") LIKE LOWER(%s) '
            'ORDER BY "name" LIMIT 20',
            (f"%{name}%",))
        return [{"id": r[0], "name": r[1], "domain": r[2], "employees": r[3]}
                for r in rows]

    def health_check(self) -> dict:
        """Return Postgres connection health + row counts."""
        try:
            self.cur.execute("SELECT 1")
            self.cur.fetchone()
            cur = self.conn.cursor()
            def cnt(table: str) -> int:
                cur.execute(f'SELECT COUNT(*) FROM "{self.schema}"."{table}"')
                r = cur.fetchone()
                return int(r[0]) if r else 0
            return {
                "ok": True, "host": _PG_HOST, "schema": self.schema,
                "opportunities": cnt("opportunity"),
                "companies": cnt("company"),
                "people": cnt("person"),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

# Load TWENTY_API_KEY from auth.json if not in environment
_auth_key = os.environ.get("TWENTY_API_KEY", "")
if not _auth_key:
    try:
        with open(HOME + "/.hermes/auth.json") as f:
            auth_data = json.load(f)
        _auth_key = auth_data.get("twenty_api_key", "")
    except Exception:
        pass
TWENTY_API_KEY = _auth_key

# Local crm.db stage → Twenty opportunity stage
# Twenty uses: NEW | SCREENING | MEETING | PROPOSAL | CLOSED_WON | CLOSED_LOST
TWENTY_STAGES = {
    "Lead":      "SCREENING",   # initial qualification
    "Qualified": "MEETING",     # discovery / calendaring
    "Call":      "MEETING",     # in-call stage
    "Proposal":  "PROPOSAL",    # proposal delivered
    "Closed":    "CLOSED_WON", # won
}
STAGE_TO_NAME = {
    "NEW":         "Lead",
    "SCREENING":   "Lead",
    "MEETING":     "Qualified",
    "PROPOSAL":    "Proposal",
    "CLOSED_WON":  "Closed",
    "CLOSED_LOST": "Closed",
}

# ── db helpers ────────────────────────────────────────────────────────────────
def get_db(path: str = CRM_DB) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SystemExit("CRM DB not found: " + path + "  (run crm-setup.sh first)")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db

def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(zip(row.keys(), row))

# ── REST client (for write operations — confirmed working) ─────────────────────
import urllib.request, urllib.error

class TwentyREST:
    """Write operations via Twenty's REST API."""
    def __init__(self, token: str = None, base_url: str = TWENTY_API_URL):
        self.base = base_url
        self.token = token if token else TWENTY_API_KEY

    def _post(self, path: str, data: dict) -> dict:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            self.base + path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("data", {})

    def _patch(self, path: str, data: dict) -> dict:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            self.base + path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("data", {})

    def create_company(self, name: str, **kw) -> dict:
        result = self._post("/rest/companies", {"name": name, **{k: v for k, v in kw.items() if v}})
        # result shape: {createCompany: {id, name, ...}}
        co = result.get("createCompany", {})
        return {"id": co.get("id"), "name": co.get("name")}

    def create_person(self, first_name: str, last_name: str,
                      email: str = None, phone: str = None,
                      company_id: str = None) -> dict:
        # companyId must be present (even null) — email/phone fields don't exist on person
        payload = {
            "name": {"firstName": first_name, "lastName": last_name},
            "companyId": company_id,
        }
        result = self._post("/rest/people", payload)
        pe = result.get("createPerson", {})
        return {"id": pe.get("id"), "name": pe.get("name")}

    def create_opportunity(self, name: str, stage: str = "NEW",
                           amount: float = None, company_id: str = None,
                           person_id: str = None) -> dict:
        payload = {"name": name, "stage": stage}
        if amount:
            payload["amount"] = {"amountMicros": int(amount * 1_000_000), "currencyCode": "USD"}
        # companyId and pointOfContactId may be required (pass null if unknown)
        if company_id:
            payload["companyId"] = company_id
        if person_id:
            payload["pointOfContactId"] = person_id
        result = self._post("/rest/opportunities", payload)
        opp = result.get("createOpportunity", {})
        return {"id": opp.get("id"), "name": opp.get("name"), "stage": opp.get("stage")}

    def update_opportunity(self, id: str, **fields) -> dict:
        result = self._patch(f"/rest/opportunities/{id}", fields)
        return result

    def add_comment(self, entity_type: str, entity_id: str, content: str):
        # Use GraphQL for comments since REST doesn't support it
        gql = """
        mutation AddComment($input: CreateCommentInput!) {
          createComment(data: $input) {
            comment { id }
          }
        }"""
        gql_client = TwentyClient()
        gql_client.token = self.token
        gql_client._post(gql, {
            "input": {
                "commentableType": entity_type.upper(),
                "commentableId": entity_id,
                "body": content,
            }
        })


class TwentyClient:
    """Read operations via Twenty GraphQL API."""
    def __init__(self, url: str = TWENTY_API_URL, token: str = None):
        self.url  = url
        self.token = token if token else TWENTY_API_KEY

    def _post(self, query: str, variables: dict = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}})
        req = urllib.request.Request(
            self.url + "/graphql",
            data=body.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def health(self) -> dict:
        try:
            result = self._post("{ people { edges { node { id } } } }")
            if "errors" in result:
                return {"ok": False, "error": result["errors"][0]["message"]}
            return {"ok": True, "workspace": "connected", "people": len(result["data"]["people"]["edges"])}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── opportunities ───────────────────────────────────────────────────────

    def list_opportunities(self) -> dict:
        """Return {stage: [opp]} grouped by stage."""
        gql = """
        query {
          opportunities {
            edges {
              node {
                id
                name
                stage
                amount { amountMicros }
                company { name }
                person { name { firstName lastName } }
              }
            }
          }
        }"""
        result = self._post(gql)
        if "errors" in result:
            return {}
        edges = result.get("data", {}).get("opportunities", {}).get("edges", [])
        grouped = {}
        for e in edges:
            n = e["node"]
            stage = n.get("stage", "UNKNOWN")
            grouped.setdefault(stage, []).append(n)
        return grouped

    def create_opportunity(self, name: str, stage: str = "PROSPECT",
                          amount: float = None, company_id: str = None,
                          person_id: str = None) -> dict:
        gql = """
        mutation CreateOpportunity($input: CreateOpportunityInput!) {
          createOpportunity(data: $input) {
            opportunity { id name stage }
          }
        }"""
        inp = {"name": name, "stage": stage}
        if amount:
            inp["amountMicros"] = int(amount * 1_000_000)
        if company_id:
            inp["companyId"] = company_id
        if person_id:
            inp["personId"] = person_id
        result = self._post(gql, {"input": inp})
        if "errors" in result:
            raise RuntimeError(result["errors"][0]["message"])
        return result["data"]["createOpportunity"]["opportunity"]

    def update_opportunity_stage(self, id: str, stage: str) -> dict:
        gql = """
        mutation UpdateOpportunity($id: UUID!, $stage: String!) {
          updateOpportunity(id: $id, data: { stage: $stage }) {
            opportunity { id stage }
          }
        }"""
        result = self._post(gql, {"id": id, "stage": stage})
        if "errors" in result:
            raise RuntimeError(result["errors"][0]["message"])
        return result["data"]["updateOpportunity"]["opportunity"]

    def get_opportunity(self, id: str) -> dict:
        gql = """
        query GetOpportunity($id: UUID!) {
          opportunity(id: $id) {
            id name stage
            amount { amountMicros }
            company { id name }
            person { id name { firstName lastName } }
          }
        }"""
        result = self._post(gql, {"id": id})
        if "errors" in result:
            raise RuntimeError(result["errors"][0]["message"])
        return result["data"]["opportunity"]

    # ── companies ───────────────────────────────────────────────────────────

    def search_companies(self, query: str, limit: int = 5) -> list:
        gql = 'query { searchResults(fullText: "%s") { edges { node { __typename ... on Company { id name domain } } } } }' % query
        result = self._post(gql)
        if "errors" in result:
            return []
        return [e["node"] for e in result.get("data", {}).get("searchResults", {}).get("edges", [])
                 if e["node"].get("__typename") == "Company"]

    def create_company(self, name: str, domain: str = None, **kw) -> dict:
        gql = """
        mutation CreateCompany($input: CreateCompanyInput!) {
          createCompany(data: $input) {
            company { id name domain }
          }
        }"""
        inp = {"name": name}
        if domain:
            inp["domain"] = domain
        result = self._post(gql, {"input": inp})
        if "errors" in result:
            raise RuntimeError(result["errors"][0]["message"])
        return result["data"]["createCompany"]["company"]

    # ── people ──────────────────────────────────────────────────────────────

    def search_people(self, query: str, limit: int = 5) -> list:
        gql = 'query { searchResults(fullText: "%s") { edges { node { __typename ... on Person { id name { firstName lastName } email } } } } }' % query
        result = self._post(gql)
        if "errors" in result:
            return []
        return [e["node"] for e in result.get("data", {}).get("searchResults", {}).get("edges", [])
                 if e["node"].get("__typename") == "Person"]

    def create_person(self, first_name: str, last_name: str,
                      email: str = None, phone: str = None,
                      company_id: str = None) -> dict:
        gql = """
        mutation CreatePerson($input: CreatePersonInput!) {
          createPerson(data: $input) {
            person { id name { firstName lastName } email }
          }
        }"""
        inp = {"name": {"firstName": first_name, "lastName": last_name}}
        if email:
            inp["email"] = email
        if phone:
            inp["phone"] = phone
        if company_id:
            inp["companyId"] = company_id
        result = self._post(gql, {"input": inp})
        if "errors" in result:
            raise RuntimeError(result["errors"][0]["message"])
        return result["data"]["createPerson"]["person"]

    # ── comments ────────────────────────────────────────────────────────────

    def add_comment(self, entity_type: str, entity_id: str, content: str):
        gql = """
        mutation AddComment($input: CreateCommentInput!) {
          createComment(data: $input) {
            comment { id }
          }
        }"""
        result = self._post(gql, {
            "input": {
                "commentableType": entity_type.upper(),
                "commentableId": entity_id,
                "body": content,
            }
        })
        if "errors" in result:
            raise RuntimeError(result["errors"][0]["message"])


# ── bridge logic ────────────────────────────────────────────────────────────────
def get_lead_by_kanban_id(db: sqlite3.Connection, kanban_task_id: str):
    return db.execute(
        "SELECT * FROM crm_leads WHERE kanban_task_id = ?",
        (kanban_task_id,)
    ).fetchone()


def bridge_lead_to_twenty(kanban_task_id: str, dry_run: bool = False,
                           client: TwentyClient = None,
                           rest: TwentyREST = None) -> dict:
    """Read lead from crm.db, upsert in Twenty via REST, write IDs back to crm.db."""
    if client is None:
        client = TwentyClient()
    if rest is None:
        rest = TwentyREST()
    db  = get_db()
    row = get_lead_by_kanban_id(db, kanban_task_id)
    if not row:
        raise SystemExit("No CRM lead for kanban task: " + kanban_task_id)

    lead = row_to_dict(row)

    # resolve local stage → Twenty stage
    stage_row = db.execute(
        "SELECT name FROM crm_pipeline_stages WHERE id = ?",
        (lead["stage_id"],)
    ).fetchone()
    stage_name  = stage_row["name"] if stage_row else "Lead"
    twenty_stage = TWENTY_STAGES.get(stage_name, "NEW")

    # company
    company_id = None
    if lead.get("company"):
        found = client.search_companies(lead["company"])
        if found:
            company_id = found[0]["id"]
        elif not dry_run:
            co = rest.create_company(name=lead["company"])
            company_id = co["id"]

    # person
    person_id = None
    if lead.get("contact_name"):
        name_parts = lead["contact_name"].split(" ", 1)
        first, last = name_parts[0], name_parts[1] if len(name_parts) > 1 else ""
        found = client.search_people(lead["contact_name"])
        if found:
            person_id = found[0]["id"]
        elif not dry_run:
            p = rest.create_person(
                first_name=first, last_name=last,
                email=lead.get("contact_email"),
                phone=lead.get("contact_phone"),
                company_id=company_id,
            )
            person_id = p["id"]

    # opportunity — with Postgres dedup (fast, no API needed)
    opp_name = (lead["contact_name"] + " — " + lead["company"]) if lead.get("company") else lead["contact_name"]
    opp_id   = None
    metadata = json.loads(lead.get("metadata") or "{}")
    existing_opp_id = metadata.get("twenty_opportunity_id")

    if existing_opp_id:
        # Already bridged — update stage if changed
        if not dry_run:
            rest.update_opportunity(existing_opp_id, stage=twenty_stage)
        opp_id = existing_opp_id
    else:
        # Dedup: check Postgres for existing opportunity with same name + company
        # (handles re-scraped leads from OpenClaw before they hit crm.db)
        dedup_found = False
        if _PG_AVAILABLE and lead.get("company"):
            try:
                pg_dedup = TwentyDB()
                dup_rows = pg_dedup._q(
                    'SELECT "id","stage" FROM "{schema}"."opportunity" '
                    'WHERE "deletedAt" IS NULL AND LOWER("name") = LOWER(%s) '
                    'AND "companyId" = %s LIMIT 1',
                    (opp_name, company_id))
                if dup_rows:
                    dup_id, dup_stage = dup_rows[0]
                    opp_id = dup_id
                    # If stage changed (e.g., moved from SCREENING → MEETING), patch it
                    if dup_stage != twenty_stage and not dry_run:
                        rest.update_opportunity(dup_id, stage=twenty_stage)
                    dedup_found = True
                    if not dry_run:
                        print(f"  [dedup] reused existing opportunity {dup_id} (stage {dup_stage} → {twenty_stage})")
            except Exception as e:
                print(f"  [dedup] check failed: {e} — proceeding to create", file=sys.stderr)

        if not dedup_found and not dry_run:
            opp = rest.create_opportunity(
                name=opp_name,
                stage=twenty_stage,
                amount=lead.get("value_estimate"),
                company_id=company_id,
                person_id=person_id,
            )
            opp_id = opp["id"]
        elif not dedup_found:
            opp_id = "<would-create>"

    # write Twenty IDs back to crm.db
    if not dry_run:
        meta_updates = {}
        if company_id:
            meta_updates["twenty_company_id"] = company_id
        if person_id:
            meta_updates["twenty_person_id"] = person_id
        if opp_id:
            meta_updates["twenty_opportunity_id"] = opp_id
        if meta_updates:
            new_meta = {**metadata, **meta_updates}
            db.execute(
                "UPDATE crm_leads SET metadata = ? WHERE id = ?",
                (json.dumps(new_meta), lead["id"])
            )
            db.commit()

    return {
        "lead_id":        lead["id"],
        "kanban_task_id": kanban_task_id,
        "opportunity_id": opp_id,
        "person_id":      person_id,
        "company_id":     company_id,
        "twenty_stage":   twenty_stage,
        "stage_name":     stage_name,
    }


def sync_all_leads(dry_run: bool = False, client: TwentyClient = None, rest: TwentyREST = None) -> list:
    if client is None:
        client = TwentyClient()
    if rest is None:
        rest = TwentyREST()
    db = get_db()
    rows = db.execute("SELECT kanban_task_id FROM crm_leads").fetchall()
    results = []
    for row in rows:
        try:
            r = bridge_lead_to_twenty(row["kanban_task_id"], dry_run=dry_run, client=client, rest=rest)
            results.append(r)
        except Exception as e:
            results.append({"kanban_task_id": row["kanban_task_id"], "error": str(e)})
    return results


# ── CLI commands ─────────────────────────────────────────────────────────────
def cmd_status(client: TwentyClient):
    h = client.health()
    if h["ok"]:
        print("Twenty API: connected  workspace=" + h["workspace"])
    else:
        print("Twenty API: DOWN — " + h["error"])
        print("  (UNAUTHENTICATED = need to set TWENTY_API_KEY from Twenty UI)")

    # Postgres direct-access check
    if _PG_AVAILABLE:
        try:
            pg = TwentyDB()
            ph = pg.health_check()
            if ph["ok"]:
                print(f"Twenty Postgres: connected  schema={ph['schema']}")
                print(f"  companies={ph['companies']}  people={ph['people']}  opportunities={ph['opportunities']}")
            else:
                print("Twenty Postgres: DOWN — " + ph["error"])
        except Exception as e:
            print("Twenty Postgres: unavailable — " + str(e))
    else:
        print("Twenty Postgres: psycopg2 not installed (pip install psycopg2-binary)")

    if os.path.exists(CRM_DB):
        db  = get_db()
        cnt = db.execute("SELECT COUNT(*) FROM crm_leads").fetchone()[0]
        print("crm.db: " + str(cnt) + " lead(s) at " + CRM_DB)
    else:
        print("crm.db: not found at " + CRM_DB)


def cmd_list(client: TwentyClient, stage: str = None, as_json: bool = False):
    # Use Postgres direct-access for read ops (no API key required)
    pg = None
    if _PG_AVAILABLE:
        try:
            pg = TwentyDB()
            by_stage = pg.get_opportunities_by_stage()
        except Exception as e:
            print(f"Postgres read failed: {e} — falling back to GraphQL")
            by_stage = client.list_opportunities()
    else:
        by_stage = client.list_opportunities()
        pg = None  # no Postgres available, can't enrich company names

    if not by_stage:
        # fall back to local CRM
        db = get_db()
        if stage:
            rows = db.execute("""
                SELECT l.kanban_task_id, l.contact_name, l.company, s.name AS stage,
                       l.value_estimate
                FROM crm_leads l
                JOIN crm_pipeline_stages s ON s.id = l.stage_id
                WHERE s.name = ?
                ORDER BY l.updated_at DESC
            """, (stage,)).fetchall()
        else:
            rows = db.execute("""
                SELECT l.kanban_task_id, l.contact_name, l.company, s.name AS stage,
                       l.value_estimate
                FROM crm_leads l
                JOIN crm_pipeline_stages s ON s.id = l.stage_id
                ORDER BY s.position, l.updated_at DESC
            """).fetchall()
        if as_json:
            print(json.dumps([dict(r) for r in rows]))
        else:
            print("CRM Pipeline:")
            for r in rows:
                val = r["value_estimate"] or 0
                print("  %-12s | %-20s | %-20s | $%,.0f" % (
                    r["stage"], r["contact_name"], r["company"] or "-", val))
        return

    if as_json:
        print(json.dumps(by_stage, indent=2, default=str))
        return

    print("Twenty Pipeline:")
    total = 0
    for s, opps in by_stage.items():
        label = STAGE_TO_NAME.get(s, s)
        print(f"\n  [{label}] {s} ({len(opps)} deal(s))")
        total += len(opps)
        for o in opps:
            amt = o.get("amountUsd", 0)
            co_id = o.get("companyId")
            # Enrich company name if available
            co_name = ""
            if co_id and pg is not None:
                try:
                    co = pg.get_company(co_id)
                    co_name = co["name"] if co else ""
                except Exception:
                    pass
            print(f"    {o['name'][:40]:<40} | {co_name[:20]:<20} | ${amt:,.0f}")
    print(f"\n  Total: {total} opportunity(ies) in Twenty (Postgres direct)")


def cmd_lead(kanban_task_id: str, client: TwentyClient, rest: TwentyREST = None, dry_run: bool = False):
    if rest is None:
        rest = TwentyREST()
    print("Bridging " + kanban_task_id + " → Twenty ...")
    result = bridge_lead_to_twenty(kanban_task_id, dry_run=dry_run, client=client, rest=rest)
    print("Done — opportunity: " + result["opportunity_id"])
    print("  stage: " + result["stage_name"] + " → " + result["twenty_stage"])


def cmd_advance(kanban_task_id: str, client: TwentyClient, rest: TwentyREST = None,
                 target_stage: str = None, notes: str = None, dry_run: bool = False):
    if rest is None:
        rest = TwentyREST()
    db  = get_db()
    row = get_lead_by_kanban_id(db, kanban_task_id)
    if not row:
        raise SystemExit("No CRM lead for: " + kanban_task_id)

    lead = row_to_dict(row)
    metadata = json.loads(lead.get("metadata") or "{}")
    opp_id   = metadata.get("twenty_opportunity_id")
    if not opp_id:
        raise SystemExit("Lead not yet bridged. Run: twenty-bridge lead " + kanban_task_id)

    cur_stage_id = lead["stage_id"]

    if not target_stage:
        nxt = db.execute("""
            SELECT name FROM crm_pipeline_stages
            WHERE position > (SELECT position FROM crm_pipeline_stages WHERE id = ?)
            ORDER BY position ASC LIMIT 1
        """, (cur_stage_id,)).fetchone()
        if not nxt:
            print("Already at final stage.")
            return
        target_stage = nxt["name"]

    twenty_stage = TWENTY_STAGES.get(target_stage)
    if not twenty_stage:
        raise SystemExit("Unknown local stage: " + target_stage)

    if not dry_run:
        rest = TwentyREST()
        rest.update_opportunity(opp_id, stage=twenty_stage)
        if notes:
            rest.add_comment("OPPORTUNITY", opp_id, notes)
        new_stage_id = db.execute(
            "SELECT id FROM crm_pipeline_stages WHERE name = ?",
            (target_stage,)
        ).fetchone()["id"]
        cur_name = db.execute(
            "SELECT name FROM crm_pipeline_stages WHERE id = ?",
            (cur_stage_id,)
        ).fetchone()["name"]
        db.execute(
            "UPDATE crm_leads SET stage_id = ?, updated_at = ? WHERE id = ?",
            (new_stage_id, int(datetime.now().timestamp()), lead["id"])
        )
        db.execute(
            "INSERT INTO crm_stage_log (lead_id, from_stage, to_stage, triggered_by, notes) "
            "VALUES (?, ?, ?, 'agent', ?)",
            (lead["id"], cur_name, target_stage, notes or "")
        )
        db.commit()

    print("Advanced " + kanban_task_id + " → " + target_stage + " (" + twenty_stage + ")")


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(prog="twenty-bridge",
        description="Bridge between local CRM (crm.db) and Twenty CRM")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Check Twenty connection + workspace health")

    p = sub.add_parser("list", help="List leads / opportunities")
    p.add_argument("--stage", help="Filter by local stage name")
    p.add_argument("--json",  action="store_true", help="JSON output")

    p = sub.add_parser("lead", help="Bridge a single lead to Twenty")
    p.add_argument("kanban_task_id")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("sync", help="Sync ALL crm.db leads to Twenty")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("advance", help="Advance a lead's Twenty stage")
    p.add_argument("kanban_task_id")
    p.add_argument("--stage", help="Target local stage name (default: next stage)")
    p.add_argument("--notes", help="Add a note with the transition")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("pg", help="Run raw SQL against Twenty Postgres (God Mode)")
    p.add_argument("sql", help="SQL query (use {schema} as placeholder for workspace schema)")
    p.add_argument("--json", action="store_true", help="Output rows as JSON")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    client = TwentyClient() if TWENTY_API_KEY else None

    if args.cmd == "status":
        if not TWENTY_API_KEY:
            print("TWENTY_API_KEY not set — get it from Twenty UI → Settings → API Keys")
        token_for_check = TWENTY_API_KEY or "dummy"
        cmd_status(TwentyClient(TWENTY_API_URL, token_for_check))

    elif args.cmd == "list":
        token = TWENTY_API_KEY or "dummy"
        cmd_list(TwentyClient(TWENTY_API_URL, token), args.stage, args.json)

    elif args.cmd == "lead":
        if not client:
            raise SystemExit("TWENTY_API_KEY required. Set: export TWENTY_API_KEY='...'")
        rest = TwentyREST()
        cmd_lead(args.kanban_task_id, client, rest, args.dry_run)

    elif args.cmd == "sync":
        if not client:
            raise SystemExit("TWENTY_API_KEY required for sync")
        rest = TwentyREST()
        for r in sync_all_leads(args.dry_run, client, rest):
            if "error" in r:
                print("FAIL " + r["kanban_task_id"] + ": " + r["error"])
            else:
                print("OK   " + r["kanban_task_id"] + " → " + r["opportunity_id"])

    elif args.cmd == "advance":
        if not client:
            raise SystemExit("TWENTY_API_KEY required. Set: export TWENTY_API_KEY='...'")
        rest = TwentyREST()
        cmd_advance(args.kanban_task_id, client, rest, args.stage, args.notes, args.dry_run)

    elif args.cmd == "pg":
        if not _PG_AVAILABLE:
            raise SystemExit("psycopg2 not installed — run: pip install psycopg2-binary")
        pg = TwentyDB()
        sql = args.sql.replace("{schema}", pg.schema)
        pg.cur.execute(sql)
        rows = pg.cur.fetchall()
        cols = [desc[0] for desc in pg.cur.description] if pg.cur.description else []
        if args.json:
            print(json.dumps([dict(zip(cols, r)) for r in rows], indent=2, default=str))
        else:
            if not rows:
                print("(empty result)")
                return
            print(" | ".join(f"{c:<30}" for c in cols))
            print("-" * (33 * len(cols)))
            for r in rows:
                print(" | ".join(f"{str(v)[:30]:<30}" for v in r))


if __name__ == "__main__":
    main()
