#!/usr/bin/env bash
# ===========================================================================
# crm-list.sh — List CRM leads by pipeline stage
#
# Shows leads in the pipeline, optionally filtered by stage.
# Default: show all stages with lead counts.
#
# Usage:
#   crm-list.sh                         # Pipeline overview with counts
#   crm-list.sh --stage "Call"          # Leads in a specific stage
#   crm-list.sh --stage "Lead" --json   # JSON output for automation
#   crm-list.sh --recent 7              # Leads created in last N days
#   crm-list.sh --kanban                # Show kanban task IDs too
# ===========================================================================
set -euo pipefail

USER_HOME="/Users/brassfieldventuresllc"
CRM_DB="${USER_HOME}/.hermes/crm.db"

usage() { sed -n 's/^# \?/  /p' "$0" | sed '1,3d' | head -12; exit 1; }

STAGE_FILTER=""
MODE="table"
RECENT_DAYS=0
SHOW_KANBAN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stage)  STAGE_FILTER="$2"; shift 2 ;;
        --json)   MODE="json";       shift ;;
        --recent) RECENT_DAYS="$2";  shift 2 ;;
        --kanban) SHOW_KANBAN=1;     shift ;;
        -h|--help) usage ;;
        *) echo "ERROR: Unknown: $1"; usage ;;
    esac
done

[[ ! -f "$CRM_DB" ]] && { echo "ERROR: CRM database not found at ${CRM_DB}"; exit 1; }

if [[ "$MODE" == "json" ]]; then
    # --- JSON mode for automation ---
    WHERE=""
    if [[ -n "$STAGE_FILTER" ]]; then
        WHERE="WHERE s.name = '${STAGE_FILTER}'"
    fi
    if [[ "$RECENT_DAYS" -gt 0 ]]; then
        CUTOFF=$(date -v-${RECENT_DAYS}d +%s 2>/dev/null || echo $(( $(date +%s) - RECENT_DAYS * 86400 )))
        [[ -z "$WHERE" ]] && WHERE="WHERE" || WHERE="${WHERE} AND"
        WHERE="${WHERE} l.created_at >= ${CUTOFF}"
    fi

    sqlite3 -json "$CRM_DB" "
        SELECT l.id, l.kanban_task_id, s.name AS stage, l.contact_name,
               l.company, l.source, l.value_estimate,
               datetime(l.created_at, 'unixepoch') AS created,
               datetime(l.updated_at, 'unixepoch') AS updated
        FROM crm_leads l
        JOIN crm_pipeline_stages s ON s.id = l.stage_id
        ${WHERE}
        ORDER BY s.position, l.updated_at DESC;
    "
    exit 0
fi

# --- Table mode ---
if [[ -n "$STAGE_FILTER" ]]; then
    # Specific stage view
    STAGE_ID=$(sqlite3 "$CRM_DB" "SELECT id, color FROM crm_pipeline_stages WHERE name = '${STAGE_FILTER}';" 2>/dev/null || true)
    [[ -z "$STAGE_ID" ]] && { echo "ERROR: Unknown stage '${STAGE_FILTER}'"; exit 1; }

    echo "━━━ Leads in Stage: ${STAGE_FILTER} ━━━"
    echo ""

    sqlite3 "$CRM_DB" "
        SELECT l.kanban_task_id, l.contact_name, l.company, l.source,
               CASE WHEN l.value_estimate IS NULL THEN '-' ELSE '$' || printf('%.0f', l.value_estimate) END,
               datetime(l.created_at, 'unixepoch')
        FROM crm_leads l
        JOIN crm_pipeline_stages s ON s.id = l.stage_id
        WHERE s.name = '${STAGE_FILTER}'
        ORDER BY l.updated_at DESC;
    " | while IFS='|' read -r TASK NAME COMPANY SOURCE VALUE CREATED; do
        if [[ "$SHOW_KANBAN" -eq 1 ]]; then
            printf "  %-22s %-20s %-18s %-12s %-10s %s\n" "$TASK" "$NAME" "${COMPANY:-}" "$SOURCE" "$VALUE" "$CREATED"
        else
            printf "  %-20s %-18s %-12s %-10s %s\n" "$NAME" "${COMPANY:-}" "$SOURCE" "$VALUE" "$CREATED"
        fi
    done

    if [[ "$SHOW_KANBAN" -eq 1 ]]; then
        printf "\n  %-22s %-20s %-18s %-12s %-10s %s\n" "Kanban Task ID" "Contact" "Company" "Source" "Value" "Created"
    fi

    echo ""
    TOTAL=$(sqlite3 "$CRM_DB" "SELECT COUNT(*) FROM crm_leads l JOIN crm_pipeline_stages s ON s.id=l.stage_id WHERE s.name='${STAGE_FILTER}';")
    echo "  Total: ${TOTAL} lead(s) in ${STAGE_FILTER}"

else
    # Pipeline overview (counts by stage)
    echo "━━━ CRM Pipeline Overview ━━━"
    echo ""

    sqlite3 "$CRM_DB" "
        SELECT s.position, s.name, s.color, COUNT(l.id) AS cnt
        FROM crm_pipeline_stages s
        LEFT JOIN crm_leads l ON l.stage_id = s.id
        GROUP BY s.id
        ORDER BY s.position;
    " | while IFS='|' read -r POS NAME COLOR CNT; do
        BARLEN=$(( CNT > 30 ? 30 : CNT ))
        BAR=""
        for ((i=0; i<BARLEN; i++)); do BAR="${BAR}▓"; done
        printf "  %-12s %3d  %s\n" "$NAME" "$CNT" "$BAR"
    done

    echo ""
    TOTAL=$(sqlite3 "$CRM_DB" "SELECT COUNT(*) FROM crm_leads;")
    echo "  Total pipeline: ${TOTAL} lead(s)"
    echo ""
    echo "  Filter: crm-list.sh --stage \"StageName\""
    echo "  Recent: crm-list.sh --recent 7"
    echo "  JSON:   crm-list.sh --json"
fi
