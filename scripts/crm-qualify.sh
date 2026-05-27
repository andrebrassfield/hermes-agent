#!/usr/bin/env bash
# ===========================================================================
# crm-qualify.sh — Check/set qualification answers for a lead
#
# Shows unanswered questions for a lead's current stage gate, or records
# answers. Used as the pre-qualification gate before advancing.
#
# Usage:
#   crm-qualify.sh <kanban-task-id>               # Show pending questions
#   crm-qualify.sh <kanban-task-id> --qid 3 --answer "Yes, within 30 days"
#
# The gate checks all required questions for the transition from the
# lead's current stage to the next stage.
# ===========================================================================
set -euo pipefail

USER_HOME="/Users/brassfieldventuresllc"
CRM_DB="${USER_HOME}/.hermes/crm.db"

usage() { sed -n '4,12p' "$0" | sed 's/^#//'; exit 1; }

# --- Parse args ---
KANBAN_TASK_ID=""
QID=""
ANSWER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --qid)    QID="$2";    shift 2 ;;
        --answer) ANSWER="$2"; shift 2 ;;
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

# --- Determine next stage ---
NEXT_STAGE=$(sqlite3 "$CRM_DB" "
    SELECT name FROM crm_pipeline_stages
    WHERE position > (SELECT position FROM crm_pipeline_stages WHERE id = ${CUR_STAGE_ID})
    ORDER BY position ASC LIMIT 1;
")

if [[ -z "$NEXT_STAGE" ]]; then
    echo "Lead is in the final stage ('${CUR_STAGE}') — no further advancement possible."
    exit 0
fi

# --- Answer mode ---
if [[ -n "$QID" && -n "$ANSWER" ]]; then
    # Verify question exists for this transition
    VALID=$(sqlite3 "$CRM_DB" "
        SELECT id FROM crm_qualification_questions
        WHERE id = ${QID} AND from_stage = '${CUR_STAGE}' AND to_stage = '${NEXT_STAGE}';
    ")
    [[ -z "$VALID" ]] && { echo "ERROR: Question #${QID} is not valid for transition ${CUR_STAGE} → ${NEXT_STAGE}"; exit 1; }

    # Check not already answered
    EXISTING=$(sqlite3 "$CRM_DB" "SELECT answer FROM crm_lead_answers WHERE lead_id = ${LEAD_ID} AND question_id = ${QID};")
    if [[ -n "$EXISTING" ]]; then
        echo "Question already answered. Overwrite? (y/N)"
        read -r CONFIRM
        [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]] && { echo "Cancelled."; exit 0; }
        sqlite3 "$CRM_DB" "DELETE FROM crm_lead_answers WHERE lead_id = ${LEAD_ID} AND question_id = ${QID};"
    fi

    sqlite3 "$CRM_DB" "
        INSERT INTO crm_lead_answers (lead_id, question_id, answer)
        VALUES (${LEAD_ID}, ${QID}, '${ANSWER//\'/\\\'}');
    "
    echo "✅ Answer recorded for question #${QID}"

    # Show remaining pending questions
    echo ""
    echo "Remaining required questions for ${CUR_STAGE} → ${NEXT_STAGE}:"
    sqlite3 "$CRM_DB" "
        SELECT q.id, q.question
        FROM crm_qualification_questions q
        LEFT JOIN crm_lead_answers a ON a.question_id = q.id AND a.lead_id = ${LEAD_ID}
        WHERE q.from_stage = '${CUR_STAGE}' AND q.to_stage = '${NEXT_STAGE}'
          AND q.required = 1
          AND a.id IS NULL
        ORDER BY q.order_num;
    " | while IFS='|' read -r QID Q; do
        echo "  [${QID}] ${Q}"
    done

    exit 0
fi

# --- Show mode (no answer) ---
echo "Pipeline stage: ${CUR_STAGE}"
echo "Transition gate: ${CUR_STAGE} → ${NEXT_STAGE}"
echo ""

# Show answered questions
echo "--- Answered Questions ---"
ANSWERS=$(sqlite3 "$CRM_DB" "
    SELECT q.id, q.question, a.answer
    FROM crm_qualification_questions q
    JOIN crm_lead_answers a ON a.question_id = q.id AND a.lead_id = ${LEAD_ID}
    WHERE q.from_stage = '${CUR_STAGE}' AND q.to_stage = '${NEXT_STAGE}'
    ORDER BY q.order_num;
")
if [[ -z "$ANSWERS" ]]; then
    echo "  (none)"
else
    echo "$ANSWERS" | while IFS='|' read -r QID Q A; do
        echo "  [${QID}] ${Q}"
        echo "        → ${A}"
    done
fi

echo ""

# Show unanswered required questions
echo "--- Pending Required Questions ---"
PENDING=$(sqlite3 "$CRM_DB" "
    SELECT q.id, q.question
    FROM crm_qualification_questions q
    LEFT JOIN crm_lead_answers a ON a.question_id = q.id AND a.lead_id = ${LEAD_ID}
    WHERE q.from_stage = '${CUR_STAGE}' AND q.to_stage = '${NEXT_STAGE}'
      AND q.required = 1
      AND a.id IS NULL
    ORDER BY q.order_num;
")
if [[ -z "$PENDING" ]]; then
    echo "  All required questions answered! Ready to advance:"
    echo "    crm-advance.sh ${KANBAN_TASK_ID}"
else
    echo "$PENDING" | while IFS='|' read -r QID Q; do
        echo "  [${QID}] ${Q}"
    done
    echo ""
    echo "Answer a question: crm-qualify.sh ${KANBAN_TASK_ID} --qid <N> --answer \"...\""
fi

# Gate status
REMAINING=$(sqlite3 "$CRM_DB" "
    SELECT COUNT(*) FROM crm_qualification_questions q
    LEFT JOIN crm_lead_answers a ON a.question_id = q.id AND a.lead_id = ${LEAD_ID}
    WHERE q.from_stage = '${CUR_STAGE}' AND q.to_stage = '${NEXT_STAGE}'
      AND q.required = 1 AND a.id IS NULL;
")
if [[ "$REMAINING" -eq 0 ]]; then
    echo ""
    echo "✅ GATE CLEARED — All required questions answered"
fi
