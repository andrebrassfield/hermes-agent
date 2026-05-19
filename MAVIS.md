# Mavis Orchestrator — Hermes Agent Repo

## Role
This repo is operated by **Mavis** (MiniMax Agent), Andre's desktop specialist agent on the MiniMax Agent team. Mavis handles all coding, automation, and execution tasks for the fleet.

## Operators
| Agent | Role | Handles |
|---|---|---|
| **Mavis** | Orchestrator / Executor | Code changes, ops, automation, debugging |
| Hermes | Dashboard / Kanban | Status visibility, task queue |
| OpenClaw | Bridge / Scheduler | Telegram dispatch, cron jobs |
| Wintermute | Daemon / Maintenance | Scheduled health checks |

## Sync & Auth
- **Fleet config**: Profiles synced to Supabase `fleet_profiles_test` / `fleet_profiles_prod` every 15 min via `~/.hermes/scripts/supabase-sync.py`
- **GitHub**: `github.com/andrebrassfield/hermes-agent` — origin push target
- **Supabase**: Management API key stored in `~/.mavis/.supabase-mgmt` (do not commit)

## MCP Servers (fleet-level)
| Name | Config key | Purpose |
|---|---|---|
| mavis-artifact | `mcp_servers.mavis-artifact` | Artifact storage |
| mavis-dispatch | `mcp_servers.mavis-dispatch` | Task dispatch |
| mavis-hermes | `mcp_servers.mavis-hermes` | Hermes gateway bridge |
| mavis-kanban | `mcp_servers.mavis-kanban` | Kanban DB access |

## Local Dev
```bash
# Profile configs live here
~/.hermes/profiles/<profile>/config.yaml

# Hermes agent source
~/.hermes/hermes-agent/

# Fleet ops dashboard plugins
~/.hermes/plugins/agent-fleet/dashboard/

# Cron scripts
~/.hermes/scripts/
```

## Branch / Release Notes
- Local fork: `main` → `origin` = `github.com/andrebrassfield/hermes-agent`
- Upstream: `NousResearch/hermes-agent` tracked separately if needed
- Last fleet sync: `supabase-sync.py` run at startup and every 15 min via `com.hermes.fleet-sync.plist`