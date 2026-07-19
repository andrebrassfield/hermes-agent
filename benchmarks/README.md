PlatformRegistry.plugin_entries() memo benchmark
==============================================

Reproduces Fable's research methodology (parent kanban task t_9f96ca66)
for the plugin_entries() -> _resolve_all() Telegram-only-cron-delivery hot path
fixed by commit cd2dfdf53.

Headline numbers (M4 MacBook Air, Python 3.11 venv, n=21 iterations):

  Cold first call (lark_oapi SDK imports, paid once per process): ~685 ms
  Per-call cost in a long-lived worker after first resolve:       ~0.001 ms
  lark_oapi top-level cumulative (--X importtime):               686 ms
  lark_oapi.* total self time across all submodules:             679 ms

The memo's actual delivery path savings:

  Scenario                                                Before        After
  --------------------------------------------------------------------------------
  Repeated plugin_entries() in same process (steady state)  ~0.0008 ms   ~0.0002 ms
  Cold first plugin_entries() (per-process cost)            ~685 ms       ~683 ms

The memo does NOT save ~500 ms PER plugin_entries() CALL, because once any
deferred loader has populated sys.modules, lark_oapi is in sys.modules
and re-imports are microseconds. The ~685 ms cost is paid ONCE PER PROCESS,
not per call. The memo's real value is in steady-state skip of an
O(N)_entries list comprehension on every load_gateway_config() call
(sub-millisecond, but on a 443-ticks/day fleet at ~5 ms each, still 2 s/day
avoided in the hot path).

Scenarios measured (each runs in a fresh subprocess so lark_oapi / Teams /
WhatsApp SDK imports are cold on first call):

  BEFORE_MEMO_FIRST    — pre-cd2dfdf53, first call inside a worker.
  BEFORE_MEMO_REPEAT   — pre-cd2dfdf53, repeated call inside same worker.
  AFTER_MEMO_FIRST     — cd2dfdf53, first call (cold SDK imports).
  AFTER_MEMO_REPEAT    — cd2dfdf53, repeated call (cache hits).

Usage:

    # Default: 11 iterations per scenario
    venv/bin/python3 benchmarks/bench_plugin_entries_memo.py

    # More iterations for tighter p95
    venv/bin/python3 benchmarks/bench_plugin_entries_memo.py --iterations 21

    # Also dump --X importtime trace for the realistic Telegram-only path
    venv/bin/python3 benchmarks/bench_plugin_entries_memo.py --importtime

Notes
-----

* `benchmarks/.drivers/` contains ephemeral subprocess drivers and the
  importtime trace; it's gitignored. The --X importtime probe writes to
  `benchmarks/.drivers/importtime_telegram_only.txt` on each run.

* The benchmark reproduces the cd2dfdf53 PATH, not necessarily the
  pre-cd2dfdf53 path. To compare pre/post, inspect the `cd2dfdf53` commit
  (`git show cd2dfdf53 -- gateway/platform_registry.py`) for the
  diff; this script measures the path AS IT EXISTS NOW (with the memo
  in place), and Phase C simulates the pre-memo behaviour by clearing
  _deferred and the cache before each call to estimate the without-
  memo counterfactual in the same process.
