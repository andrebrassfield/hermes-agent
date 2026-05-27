#!/usr/bin/env bash
# ===========================================================================
# crm-setup.sh — Initialize CRM pipeline database and seed stages
#
# Creates crm.db with schema for pipeline stages, leads, qualification
# questions, answers, and stage-change audit log.
#
# Safe to re-run — uses IF NOT EXISTS / INSERT OR IGNORE.
# ===========================================================================
set -euo pipefail

USER_HOME="/Users/brassfieldventuresllc"
CRM_DB="${USER_HOME}/.hermes/crm.db"

cd "$(dirname "$0")"

echo "[crm-setup] Initializing CRM database at ${CRM_DB}..."

sqlite3 "${CRM_DB}" <<'SQL'
-- Pipeline stage definitions (the 5 stages of the sales pipeline)
CREATE TABLE IF NOT EXISTS crm_pipeline_stages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,
    position         INTEGER NOT NULL UNIQUE,   -- 1-5 ordering
    color            TEXT    NOT NULL DEFAULT '#808080',
    default_assignee TEXT,                        -- optional default kanban assignee
    created_at       INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Lead records linked to kanban task IDs
CREATE TABLE IF NOT EXISTS crm_leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kanban_task_id  TEXT    NOT NULL UNIQUE,      -- FK into kanban.db tasks.id
    stage_id        INTEGER NOT NULL REFERENCES crm_pipeline_stages(id),
    contact_name    TEXT    NOT NULL,
    contact_email   TEXT,
    contact_phone   TEXT,
    company         TEXT,
    source          TEXT,                          -- referral, cold-outreach, inbound, etc.
    value_estimate  REAL,                          -- estimated deal value
    notes           TEXT,
    metadata        TEXT,                          -- JSON blob for extra fields
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Qualification questions for stage gates
CREATE TABLE IF NOT EXISTS crm_qualification_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_stage  TEXT    NOT NULL,                  -- stage name this question gates from
    to_stage    TEXT    NOT NULL,                  -- stage name this question gates to
    question    TEXT    NOT NULL,
    required    INTEGER NOT NULL DEFAULT 1,        -- 1=must answer, 0=optional
    order_num   INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Answers to qualification questions per lead
CREATE TABLE IF NOT EXISTS crm_lead_answers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES crm_leads(id),
    question_id INTEGER NOT NULL REFERENCES crm_qualification_questions(id),
    answer      TEXT    NOT NULL,
    answered_by TEXT    NOT NULL DEFAULT 'agent',
    answered_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(lead_id, question_id)
);

-- Audit log for stage transitions
CREATE TABLE IF NOT EXISTS crm_stage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id       INTEGER NOT NULL REFERENCES crm_leads(id),
    from_stage    TEXT,                             -- NULL on first entry
    to_stage      TEXT    NOT NULL,
    triggered_by  TEXT    NOT NULL,                  -- agent, user, system
    notes         TEXT,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_leads_stage    ON crm_leads(stage_id);
CREATE INDEX IF NOT EXISTS idx_leads_kanban   ON crm_leads(kanban_task_id);
CREATE INDEX IF NOT EXISTS idx_answers_lead   ON crm_lead_answers(lead_id);
CREATE INDEX IF NOT EXISTS idx_stage_log_lead ON crm_stage_log(lead_id, created_at);

-- Prevent duplicate qualification questions
CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_unique
    ON crm_qualification_questions(from_stage, to_stage, question);
SQL

echo "[crm-setup] Seeding pipeline stages..."

# Seed stages (idempotent — INSERT OR IGNORE on name UNIQUE)
sqlite3 "${CRM_DB}" <<'SQL'
INSERT OR IGNORE INTO crm_pipeline_stages (name, position, color, default_assignee) VALUES
    ('Lead',      1, '#6B7280', 'outreach-agent'),
    ('Qualified', 2, '#3B82F6', 'lead-engineer'),
    ('Call',      3, '#8B5CF6', 'lead-engineer'),
    ('Proposal',  4, '#F59E0B', 'lead-engineer'),
    ('Closed',    5, '#10B981', NULL);
SQL

echo "[crm-setup] Seeding qualification questions..."

# Pre-qualification: Lead → Qualified
sqlite3 "${CRM_DB}" <<'SQL'
INSERT OR IGNORE INTO crm_qualification_questions (from_stage, to_stage, question, required, order_num) VALUES
    ('Lead', 'Qualified', 'What is your approximate budget range for this project?',              1, 1),
    ('Lead', 'Qualified', 'What is your decision timeline (weeks/months)?',                        1, 2),
    ('Lead', 'Qualified', 'Who else is involved in the decision-making process?',                  1, 3),
    ('Lead', 'Qualified', 'What specific problem or goal are you trying to achieve?',              1, 4),
    ('Lead', 'Qualified', 'Have you allocated budget for this initiative?',                        1, 5);

-- Pre-call: Qualified → Call (calendar booking gate)
INSERT OR IGNORE INTO crm_qualification_questions (from_stage, to_stage, question, required, order_num) VALUES
    ('Qualified', 'Call', 'What specific outcomes would you like from our conversation?',           1, 1),
    ('Qualified', 'Call', 'Have you used AI or automation tools before? If so, which ones?',       1, 2),
    ('Qualified', 'Call', 'What is the primary objection holding you back from moving forward?',   1, 3),
    ('Qualified', 'Call', 'What would need to be true for you to move forward this quarter?',      1, 4),
    ('Qualified', 'Call', 'Do you prefer a phone call or video meeting?',                           1, 5);
SQL

echo "[crm-setup] Verifying..."

sqlite3 "${CRM_DB}" "SELECT id, name, position, color FROM crm_pipeline_stages ORDER BY position;"
echo ""
sqlite3 "${CRM_DB}" "SELECT id, from_stage, '→', to_stage, substr(question,1,60) AS q FROM crm_qualification_questions ORDER BY from_stage, order_num;"

echo ""
echo "[crm-setup] Done. CRM database ready at ${CRM_DB}"
