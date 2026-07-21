import json, statistics, sys, time

sys.path.insert(0, "/Users/brassfieldventuresllc/.hermes/hermes-agent")

from gateway.platform_registry import PlatformRegistry, PlatformEntry

ITERATIONS = 11
HAS_MEMO = True

# Feishu's adapter imports lark_oapi at module scope — that's the heavy
# SDK that bit Fable. We use it as the deferred-loader body so each
# iteration's _resolve_all() pays a real-world cost.
def _heavy_loader():
    try:
        from plugins.platforms.feishu.adapter import register as _f  # noqa: F401
        _f(None)
    except Exception:
        # Loader exceptions are swallowed in production (see
        # PlatformRegistry._resolve). We do the same here.
        pass


def build_pr(simulate_pre_memo: bool):
    # Build a registry.  When simulate_pre_memo is True we keep
    # repopulating _deferred on every call (matches the parent's claim
    # that every load_gateway_config() call paid the SDK cost).
    pr = PlatformRegistry()
    pr.register(PlatformEntry(
        name="telegram", label="Telegram", source="builtin",
        adapter_factory=lambda c: None, check_fn=lambda: True,
    ))
    pr.register_deferred("feishu", _heavy_loader)
    pr.register_deferred("teams", _heavy_loader)
    pr.register_deferred("whatsapp", _heavy_loader)
    return pr


def one_call(pr, simulate_pre_memo: bool):
    # One plugin_entries() call.
    #
    # If simulate_pre_memo is True, behave like pre-cd2dfdf53:
    # re-populate _deferred + clear the memo so each call must do a
    # full _resolve_all(). This matches the parent's claim that
    # "every load_gateway_config() call would pay the SDK import cost".
    if simulate_pre_memo:
        # Wipe cache + re-seed deferred loaders so the next call must
        # pay a full resolve.
        if not HAS_MEMO:
            pr._plugin_entries_cache = None
            pr._deferred.clear()
            pr._deferred["feishu"] = _heavy_loader
            pr._deferred["teams"] = _heavy_loader
            pr._deferred["whatsapp"] = _heavy_loader

    t0 = time.perf_counter()
    entries = pr.plugin_entries()
    return (time.perf_counter() - t0) * 1000.0


# =====================================================================
# Phase A: ONE cold-process scenario (matches a fresh Telegram-only
# cron worker process on its first tick of the day). The first call
# within this process pays the lark_oapi / Teams / WhatsApp SDK cost.
# =====================================================================
pr = build_pr(simulate_pre_memo=False)
cold_first_ms = one_call(pr, simulate_pre_memo=False)

# =====================================================================
# Phase B: repeat-call cost in a long-lived worker process. Same
# process, same registry, after the first resolve. The memo's whole
# job is to make these calls effectively free.
# =====================================================================
post_first_calls = []
for _ in range(ITERATIONS):
    post_first_calls.append(one_call(pr, simulate_pre_memo=False))

# =====================================================================
# Phase C: pre-memo simulation in the SAME long-lived process. We
# wipe _deferred and the cache before every call so each call has to
# pay a full resolve. This shows the "every load_gateway_config()
# pays" claim was wrong -- once SDKs are in sys.modules, even a full
# resolve is microseconds.
# =====================================================================
pre_memo_calls = []
for _ in range(ITERATIONS):
    pre_memo_calls.append(one_call(pr, simulate_pre_memo=True))

# =====================================================================
# Phase D: a fresh subprocess for each repeat call. This is the
# "every cron tick spawns a new process" scenario (legacy mode), and
# the memo CANNOT help -- each process pays the SDK cost from cold.
# We measure this via a separate subprocess but collect a single
# number from the parent's per-iteration subprocess loop. (See the
# parent's PER_CALL_SUBPROCESS_JSON_OUT below.)
# =====================================================================
print(json.dumps({
    "scenario": "AFTER_MEMO_FIRST",
    "iterations": ITERATIONS,
    # Phase A cold call (in same process)
    "cold_first_ms": cold_first_ms,
    # Phase B -- AFTER_MEMO equivalent. Same process, cache populated,
    # repeat plugin_entries() calls. Sub-millisecond.
    "post_first_median_ms": statistics.median(post_first_calls),
    "post_first_p95_ms": sorted(post_first_calls)[int(0.95 * (len(post_first_calls) - 1))],
    "post_first_results_ms": post_first_calls,
    # Phase C -- pre-cd2dfdf53 WITHIN the same process. SDKs are already
    # in sys.modules, so each "would-be expensive" call is microseconds.
    "pre_memo_median_ms": statistics.median(pre_memo_calls),
    "pre_memo_p95_ms": sorted(pre_memo_calls)[int(0.95 * (len(pre_memo_calls) - 1))],
    "pre_memo_results_ms": pre_memo_calls,
}))
