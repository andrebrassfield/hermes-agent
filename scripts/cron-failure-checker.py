#!/usr/bin/env python3
"""
cron-failure-checker.sh equivalent in Python.
Reads ~/.hermes/cron/jobs.json, creates idempotent kanban tasks for failures.
Quiet when healthy, noisy when action needed.
"""
import subprocess
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES = "/Users/brassfieldventuresllc/.hermes/hermes-agent/venv/bin/hermes"
JOBS_JSON = "/Users/brassfieldventuresllc/.hermes/cron/jobs.json"
OUTPUT_DIR = Path("/Users/brassfieldventuresllc/.hermes/cron/output")
HEARTBEAT = Path("/Users/brassfieldventuresllc/.hermes/cron/heartbeat.log")
USER_HOME = Path("/Users/brassfieldventuresllc")

def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1

def kanban_create(title, body_dict, ikey, assignee="ares", max_runtime=600):
    """Create kanban task idempotently. Returns True if created or already existed."""
    body = json.dumps(body_dict)
    cmd = [
        HERMES, "kanban", "create", title,
        "--body", body,
        "--assignee", assignee,
        "--workspace", "scratch",
        "--idempotency-key", ikey,
        "--max-runtime", str(max_runtime),
    ]
    out, code = run(cmd)
    return code == 0 or "already" in out.lower() or "duplicate" in out.lower()

def main():
    report = []
    tasks_created = 0

    # Load jobs.json
    try:
        with open(JOBS_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        jobs = raw if isinstance(raw, list) else raw.get("jobs", [])
    except Exception as e:
        print(f"ERROR: could not read jobs.json: {e}", file=sys.stderr)
        sys.exit(0)  # Don't spam on structural issue

    now = datetime.now(timezone.utc)

    # Categorize
    stalled, errors, paused = [], [], []

    for j in jobs:
        sid = j.get("job_id", "")
        name = j.get("name", "?")

        # Stalled active
        if j.get("state") == "active" and j.get("last_run_at"):
            try:
                ts = j.get("last_run_at", "")
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                rt = datetime.fromisoformat(ts)
                age = (now - rt).total_seconds() / 60
                if age > 10:
                    stalled.append({"id": sid, "name": name, "age_min": int(age)})
            except Exception:
                pass

        # Error
        if j.get("last_status") == "error":
            errors.append({
                "id": sid, "name": name,
                "last_error": str(j.get("last_delivery_error", ""))[:120]
            })

        # Paused/disabled
        if j.get("state") == "paused" or not j.get("enabled", True):
            paused.append({"id": sid, "name": name, "state": j.get("state", "?")})

    # Create tasks for stalled
    for s in stalled:
        created = kanban_create(
            f"Fix stalled cron: {s['name']} ({s['age_min']}min)",
            {"type": "stalled_active_job", "job_id": s["id"], "age_min": s["age_min"], "name": s["name"]},
            f"cron-stalled-{s['id']}",
            max_runtime=300,
        )
        if created:
            tasks_created += 1
            print(f"[stalled] created: {s['name']}", file=sys.stderr)

    if stalled:
        report.append(f"🔴 STALLED ACTIVE JOBS ({len(stalled)}):")
        for s in stalled:
            report.append(f"  - {s['name']} active {s['age_min']}min")
        report.append("")

    # Create tasks for errors
    for e in errors:
        created = kanban_create(
            f"Fix cron failure: {e['name']}",
            {"type": "cron_error", "job_id": e["id"], "name": e["name"], "last_error": e["last_error"]},
            f"cron-error-{e['id']}",
            max_runtime=900,
        )
        if created:
            tasks_created += 1
            print(f"[error] created: {e['name']}", file=sys.stderr)

    if errors:
        report.append(f"🔴 ERRORING JOBS ({len(errors)}):")
        for e in errors:
            err = e["last_error"][:60] if e["last_error"] else ""
            report.append(f"  - {e['name']} {err}")
        report.append("")

    # Paused (informational)
    if paused:
        report.append(f"⚠️  PAUSED/DISABLED ({len(paused)}):")
        for p in paused:
            report.append(f"  - {p['name']} ({p['state']})")
        report.append("")

    # Error patterns in recent output
    try:
        cutoff = datetime.now().timestamp() - 7200  # 2 hours
        recent_files = [f for f in OUTPUT_DIR.rglob("*") if f.is_file() and f.stat().st_mtime > cutoff]
        error_files = []
        for f in recent_files[:20]:
            try:
                content = f.read_text(errors="ignore")
                if any(content.startswith(pat) for pat in ("ERROR", "FAIL", "EXCEPTION", "PANIC")):
                    error_files.append(str(f))
            except Exception:
                pass
        if error_files:
            report.append("🔴 ERROR PATTERN IN OUTPUT FILES:")
            for ef in error_files:
                report.append(f"  {ef}")
            report.append("")
    except Exception as e:
        print(f"[warn] output scan failed: {e}", file=sys.stderr)

    # Heartbeat
    try:
        if HEARTBEAT.exists():
            last_hb = HEARTBEAT.read_text(encoding="utf-8").strip().split("\n")[-1][:16]
            from time import mktime
            import subprocess
            hr = subprocess.run(["date", "-j", "-f", "%Y-%m-%d %H:%M", last_hb, "+%s"],
                               capture_output=True, text=True)
            if hr.returncode == 0:
                last_epoch = int(hr.stdout.strip())
                now_epoch = int(datetime.now().timestamp())
                diff_min = (now_epoch - last_epoch) // 60
                if diff_min > 15:
                    report.append(f"🔴 HEARTBEAT LOST — last: {last_hb} ({diff_min} min ago)")
                    report.append("")
        else:
            report.append("⚠️  NO heartbeat.log found")
            report.append("")
    except Exception as e:
        print(f"[warn] heartbeat check failed: {e}", file=sys.stderr)

    # Disk
    try:
        hr = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
        if hr.returncode == 0:
            disk_pct = int(hr.stdout.strip().split("\n")[-1].split()[-1].rstrip("%"))
            if disk_pct > 90:
                report.append(f"🔴 DISK CRITICAL: {disk_pct}% used")
                report.append("")
            elif disk_pct > 85:
                report.append(f"⚠️  Disk usage: {disk_pct}%")
                report.append("")
    except Exception:
        pass

    # Memory
    try:
        hr = subprocess.run(["memory_pressure"], capture_output=True, text=True)
        if hr.returncode == 0:
            for line in hr.stdout.split("\n"):
                if "System-wide memory free percentage" in line:
                    mem_free = int(line.split()[-1].rstrip("%"))
                    if mem_free < 10:
                        report.append(f"🔴 MEMORY CRITICAL: {mem_free}% free")
                        report.append("")
                    elif mem_free < 20:
                        report.append(f"⚠️  Memory: {mem_free}% free")
                        report.append("")
                    break
    except Exception:
        pass

    # Output
    if report:
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"=== Cron Health Check — {now_str} ===")
        print("\n".join(report))
        print(f"Tasks created this run: {tasks_created}")
        sys.exit(1)
    else:
        # Silent — all OK
        sys.exit(0)

if __name__ == "__main__":
    main()
