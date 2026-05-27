#!/usr/bin/env bash
# ===========================================================================
# crm-advance.sh — Advance a CRM lead to the next pipeline stage
#
# Moves a lead through the pipeline: stages are advanced one at a time
# (Lead → Qualified → Call → Proposal → Closed). Before advancing,
# checks the qualification gate — all required questions for the
# transition must be answered.
#
# Usage:
#   crm-advance.sh <kanban-task-id>              # Advance to next stage
#   crm-advance.sh <kanban-task-id> --force       # Skip gate check
#   crm-advance.sh <kanban-task-id> --stage "Call" # Jump to specific stage
#   crm-advance.sh <kanban-task-id> --notes "..."  # Add log notes
#
# On advance, posts a kanban comment and optionally updates the
# kanban task status (for Closed → done, others → running).
# ===========================================================================
set -euo pipefail

USER_HOME="/Users/brassfieldventuresllc"
CRM_DB="${USER_HOME}/.hermes/crm.db"

usage() {
    sed -n 's/^# \?/  /p' "$0" | sed '1,3d' | head -20
    exit 1
}

# --- Parse args ---
KANBAN_TASK_ID=""
FORCE=0
TARGET_STAGE=""
NOTES=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)  FORCE=1;     shift ;;
        --stage)  TARGET_STAGE="$2"; shift 2 ;;
        --notes)  NOTES="$2";  shift 2 ;;
        -h|--help) usage ;;
        -*)
            echo "ERROR: Unknown option $1"
            usage
            ;;
        *)
            [[ -z "$KANBAN_TASK_ID" ]] && KANBAN_TASK_ID="$1" || { echo "ERROR: Unexpected: $1"; usage; }
            shift
            ;;
    esac
done

[[ -z "$KANBAN_TASK_ID" ]] && { echo "ERROR: kanban-task-id required"; usage; }

# --- Resolve lead ---
LEAD_ROW=$(sqlite3 "$CRM_DB" "SELECT id, stage_id FROM crm_leads WHERE kanban_task_id = '${KANBAN_TASK_ID}';")
[[ -z "$LEAD_ROW" ]] && { echo "ERROR: Kanban task ${KANBAN_TASK_ID} is not a CRM lead"; exit 1; }

LEAD_ID=$(echo "$LEAD_ROW" | cut -d'|' -f1)
CUR_STAGE_ID=$(echo "$LEAD_ROW" | cut -d'|' -f2)
CUR_STAGE=$(sqlite3 "$CRM_DB" "SELECT name FROM crm_pipeline_stages WHERE id = ${CUR_STAGE_ID};")

# --- Determine target stage ---
if [[ -n "$TARGET_STAGE" ]]; then
    TARGET_ID=$(sqlite3 "$CRM_DB" "SELECT id FROM crm_pipeline_stages WHERE name = '${TARGET_STAGE}';")
    [[ -z "$TARGET_ID" ]] && { echo "ERROR: Unknown stage '${TARGET_STAGE}'"; exit 1; }
    CUR_POS=$(sqlite3 "$CRM_DB" "SELECT position FROM crm_pipeline_stages WHERE id = ${CUR_STAGE_ID};")
    TGT_POS=$(sqlite3 "$CRM_DB" "SELECT position FROM crm_pipeline_stages WHERE id = ${TARGET_ID};")
    [[ "$TARGET_ID" -eq "$CUR_STAGE_ID" ]] && { echo "Lead is already in stage '${CUR_STAGE}'"; exit 0; }
    [[ "$TGT_POS" -le "$CUR_POS" ]] && { echo "ERROR: Cannot go backward from ${CUR_STAGE} (pos ${CUR_POS}) to ${TARGET_STAGE} (pos ${TGT_POS})"; exit 1; }
else
    # Default: advance to next position
    TARGET_ID=$(sqlite3 "$CRM_DB" "
        SELECT id FROM crm_pipeline_stages
        WHERE position > (SELECT position FROM crm_pipeline_stages WHERE id = ${CUR_STAGE_ID})
        ORDER BY position ASC LIMIT 1;
    ")
    [[ -z "$TARGET_ID" ]] && { echo "Lead '${CUR_STAGE}' is the final stage — already at end of pipeline."; exit 0; }
    TARGET_STAGE=$(sqlite3 "$CRM_DB" "SELECT name FROM crm_pipeline_stages WHERE id = ${TARGET_ID};")
fi

# --- Qualification gate (unless --force or jump to non-adjacent) ---
if [[ "$FORCE" -eq 0 ]]; then
    # Only check gate for adjacent transition
    CUR_POS=$(sqlite3 "$CRM_DB" "SELECT position FROM crm_pipeline_stages WHERE id = ${CUR_STAGE_ID};")
    TGT_POS=$(sqlite3 "$CRM_DB" "SELECT position FROM crm_pipeline_stages WHERE id = ${TARGET_ID};")

    if [[ "$((TGT_POS - CUR_POS))" -eq 1 ]]; then
        PENDING=$(sqlite3 "$CRM_DB" "
            SELECT COUNT(*) FROM crm_qualification_questions q
            LEFT JOIN crm_lead_answers a ON a.question_id = q.id AND a.lead_id = ${LEAD_ID}
            WHERE q.from_stage = '${CUR_STAGE}' AND q.to_stage = '${TARGET_STAGE}'
              AND q.required = 1 AND a.id IS NULL;
        ")
        if [[ "$PENDING" -gt 0 ]]; then
            echo "❌ GATE BLOCKED — ${PENDING} required question(s) unanswered for ${CUR_STAGE} → ${TARGET_STAGE}"
            echo "  View pending: crm-qualify.sh ${KANBAN_TASK_ID}"
            echo "  Override:     crm-advance.sh ${KANBAN_TASK_ID} --force"
            exit 1
        fi
    fi
fi

# --- Execute advancement ---
TIMESTAMP=$(date +%s)
sqlite3 "$CRM_DB" "
    UPDATE crm_leads SET stage_id = ${TARGET_ID}, updated_at = ${TIMESTAMP}
    WHERE id = ${LEAD_ID};
"

sqlite3 "$CRM_DB" "
    INSERT INTO crm_stage_log (lead_id, from_stage, to_stage, triggered_by, notes)
    VALUES (${LEAD_ID}, '${CUR_STAGE}', '${TARGET_STAGE}',
            'agent', '${NOTES//\'/\\\'}');
"

# --- Update kanban task ---
# For closed/won, mark kanban task done; for others, mark running
if [[ "$TARGET_STAGE" == "Closed" ]]; then
    hermes kanban comment "${KANBAN_TASK_ID}" \
        "✅ **CRM Pipeline:** Advanced to **${TARGET_STAGE}** ✅ (from ${CUR_STAGE})" 2>/dev/null || true
    echo "✅ Lead advanced: ${CUR_STAGE} → ${TARGET_STAGE} (kanban task should be marked done)"
    echo "  hermes kanban complete ${KANBAN_TASK_ID} --summary 'Deal closed'"
else
    hermes kanban comment "${KANBAN_TASK_ID}" \
        "🔄 **CRM Pipeline:** Advanced to **${TARGET_STAGE}** (from ${CUR_STAGE})${NOTES:+ — ${NOTES}}" 2>/dev/null || true
    echo "✅ Lead advanced: ${CUR_STAGE} → ${TARGET_STAGE}"
fi

# --- Bridge to Twenty CRM (if API key is set) ---
HERMES_VENV_PYTHON="${USER_HOME}/.hermes/hermes-agent/venv/bin/python3"
if [[ -n "${TWENTY_API_KEY:-}" ]]; then
    TWENTY_BRIDGE_OUTPUT=$("$HERMES_VENV_PYTHON" "${USER_HOME}/.hermes/hermes-agent/scripts/twenty-bridge.py" advance "${KANBAN_TASK_ID}" ${NOTES:+--notes "${NOTES}"} 2>&1) && \
        echo "Twenty: ${TWENTY_BRIDGE_OUTPUT}" || \
        echo "Twenty bridge skipped (workspace not initialized)"
fi

# Show pipeline progress
echo ""
sqlite3 "$CRM_DB" "
    SELECT s.name, CASE WHEN s.id = ${TARGET_ID} THEN '◀ YOU ARE HERE' ELSE '' END
    FROM crm_pipeline_stages s
    ORDER BY s.position;
" | while IFS='|' read -r NAME MARKER; do
    printf "  %-12s %s\n" "$NAME" "$MARKER"
done
