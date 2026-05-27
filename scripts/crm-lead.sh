#!/usr/bin/env bash
# ===========================================================================
# crm-lead.sh — Register a kanban task as a CRM lead
#
# Creates a local CRM record AND bridges it to Twenty CRM.
# Local crm.db = qualification gates + audit log.
# Twenty = live opportunity/contact/company in the CRM.
#
# Usage:
#   ./crm-lead.sh <kanban-task-id> --name "Name" [--email "e"] [--phone "p"] \
#       [--company "Co"] [--source "cold-outreach"] [--value 5000]
#
# Dependencies:
#   - Hermes kanban CLI (hermes kanban)
#   - sqlite3 (macOS built-in)
#   - CRM database at ~/.hermes/crm.db (initialized by crm-setup.sh)
#   - twenty-bridge.py (bridges lead → Twenty CRM)
#     Path: ~/.hermes/hermes-agent/scripts/twenty-bridge.py
#     Run via hermes venv python for psycopg2 support
# ===========================================================================
set -euo pipefail

USER_HOME="/Users/brassfieldventuresllc"
CRM_DB="${USER_HOME}/.hermes/crm.db"
HERMES_CLI="${USER_HOME}/.local/bin/hermes"

usage() {
    sed -n 's/^# \?/  /p' "$0" | sed '1,3d' | head -20
    echo ""
    echo "Example:"
    echo "  ./crm-lead.sh t_9eba7026 --name \"Jay Downing\" --company \"Flyer Homes\" --source cold-outreach"
    exit 1
}

# --- Parse args ---
KANBAN_TASK_ID=""
CONTACT_NAME=""
CONTACT_EMAIL=""
CONTACT_PHONE=""
COMPANY=""
SOURCE=""
VALUE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)    CONTACT_NAME="$2";     shift 2 ;;
        --email)   CONTACT_EMAIL="$2";    shift 2 ;;
        --phone)   CONTACT_PHONE="$2";    shift 2 ;;
        --company) COMPANY="$2";          shift 2 ;;
        --source)  SOURCE="$2";           shift 2 ;;
        --value)   VALUE="$2";            shift 2 ;;
        -h|--help) usage ;;
        -*)
            echo "ERROR: Unknown option $1"
            usage
            ;;
        *)
            if [[ -z "$KANBAN_TASK_ID" ]]; then
                KANBAN_TASK_ID="$1"
            else
                echo "ERROR: Unexpected argument: $1"
                usage
            fi
            shift
            ;;
    esac
done

if [[ -z "$KANBAN_TASK_ID" || -z "$CONTACT_NAME" ]]; then
    echo "ERROR: kanban-task-id and --name are required"
    usage
fi

# --- Validate kanban task exists ---
KANBAN_JSON=$(hermes kanban show "$KANBAN_TASK_ID" --json 2>/dev/null || true)
if [[ -z "$KANBAN_JSON" ]]; then
    echo "ERROR: Kanban task $KANBAN_TASK_ID not found or hermes CLI unavailable"
    exit 1
fi

# --- Validate CRM DB exists ---
if [[ ! -f "$CRM_DB" ]]; then
    echo "ERROR: CRM database not found at ${CRM_DB}"
    echo "  Run crm-setup.sh first."
    exit 1
fi

# --- Check not already a lead ---
EXISTING=$(sqlite3 "$CRM_DB" "SELECT id FROM crm_leads WHERE kanban_task_id = '${KANBAN_TASK_ID}';")
if [[ -n "$EXISTING" ]]; then
    echo "ERROR: Kanban task ${KANBAN_TASK_ID} is already a CRM lead (lead id: ${EXISTING})"
    exit 1
fi

# --- Get first stage id ---
STAGE_ID=$(sqlite3 "$CRM_DB" "SELECT id FROM crm_pipeline_stages ORDER BY position ASC LIMIT 1;")
STAGE_NAME=$(sqlite3 "$CRM_DB" "SELECT name FROM crm_pipeline_stages WHERE id = ${STAGE_ID};")

# --- Insert lead ---
LEAD_ID=$(sqlite3 "$CRM_DB" "
    INSERT INTO crm_leads (kanban_task_id, stage_id, contact_name, contact_email,
                           contact_phone, company, source, value_estimate)
    VALUES ('${KANBAN_TASK_ID}', ${STAGE_ID}, '${CONTACT_NAME//\'/\\\'}',
            '${CONTACT_EMAIL//\'/\\\'}', '${CONTACT_PHONE//\'/\\\'}',
            '${COMPANY//\'/\\\'}', '${SOURCE//\'/\\\'}',
            ${VALUE:-NULL});
    SELECT last_insert_rowid();
")

# --- Log stage entry ---
sqlite3 "$CRM_DB" "
    INSERT INTO crm_stage_log (lead_id, from_stage, to_stage, triggered_by, notes)
    VALUES (${LEAD_ID}, NULL, '${STAGE_NAME}', 'agent', 'Lead created from kanban task ${KANBAN_TASK_ID}');
"

# --- Update kanban task body with CRM stage marker ---
# We use the hermes CLI to comment on the task
hermes kanban comment "${KANBAN_TASK_ID}" \
    "🏷 **CRM Lead** | Stage: **${STAGE_NAME}** | Contact: ${CONTACT_NAME}${COMPANY:+ (@ ${COMPANY})}${SOURCE:+ | Source: ${SOURCE}}

To advance: \`crm-advance.sh ${KANBAN_TASK_ID}\`" 2>/dev/null || true

# --- Bridge to Twenty CRM (if API key is set) ---
HERMES_VENV_PYTHON="${USER_HOME}/.hermes/hermes-agent/venv/bin/python3"
if [[ -n "${TWENTY_API_KEY:-}" ]]; then
    TWENTY_BRIDGE_OUTPUT=$("$HERMES_VENV_PYTHON" "${USER_HOME}/.hermes/hermes-agent/scripts/twenty-bridge.py" lead "${KANBAN_TASK_ID}" 2>&1) && \
        echo "Twenty: ${TWENTY_BRIDGE_OUTPUT}" || \
        echo "Twenty bridge skipped (not configured or workspace not initialized)"
fi

echo "✅ Lead #${LEAD_ID} created — kanban task ${KANBAN_TASK_ID} → CRM stage '${STAGE_NAME}'"
