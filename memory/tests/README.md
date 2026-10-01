# Memory System Tests — Agent Guide

**Read this before editing anything under `~/.silly-memory/`.** This is a
regression suite. Its job is to make sure bugs we already fixed never come back.

## How to run

```bash
python3 ~/.silly-memory/bin/memory selftest
# or, equivalently:
python3 -m unittest discover -s ~/.silly-memory/tests -p "test_*.py" -v
```

The suite is **stdlib-only** (`unittest`). Do not add `pytest` or any external
dependency — it must run anywhere `python3` exists, with no install step.

### Integration tests

`memory/tests/integration/` holds tests that require a live network connection
and valid credentials (currently: a working `cursor-agent` binary with an
authenticated Cursor account). They are **opt-in** and never execute as part of
the default `memtest` / `selftest` invocation.

`integration/` is a package, so `unittest discover` **does** recurse into it and
collect its tests. Each test gates itself and skips unless both variables are
set:

```bash
MEMORY_ALLOW_NETWORK=1 MEMORY_RUN_INTEGRATION_TESTS=1 \
  python3 -m unittest discover -s memory/tests -p "test_cursor_integration*" -v
```

In a default run they show up as `skipped` with the reason. If your credentials
are missing, the keychain is locked, or the network is unreachable, the test
calls `self.skipTest(...)` with a descriptive reason rather than failing.

**When to add a test here**: only when the behavior under test genuinely
requires a live `cursor-agent` round-trip or other external credential. Anything
that can be stubbed or faked belongs in the standard suite under `memory/tests/`.

## The one rule

**Every time you fix a bug, add a test that fails before your fix and passes
after.** A bug without a test will eventually come back. The existing tests each
pin a real bug from this project's history (see the table at the bottom).

Workflow for any change:

1. Run `memory selftest` first — confirm it's green before you start.
2. Make your change.
3. If you fixed a bug, add a test reproducing it in `test_memory.py`.
4. Run `memory selftest` again — it must be green before you're done.

## How isolation works (do not break this)

Tests must **never touch the real store** at `~/.silly-memory/_global` or any
real workspace store. Isolation relies on one mechanism:

- `config.memory_home()` honors the `SILLY_MEMORY_HOME` env var. When set, the
  whole system (config, stores, index, queues) lives under that path instead of
  `~/.silly-memory`. Clear it (and `MEMORY_BIN`) before a test sets its own home.
- `MemoryTestBase.setUp()` sets `SILLY_MEMORY_HOME` to a fresh `tempfile`
  directory, copies the real `config.json` into it (so bank routing + token caps
  are realistic), and creates a temp workspace with a `.git/` dir.
- `tearDown()` removes both temp dirs and unsets the env var.

Consequences for writing tests:

- **Always** subclass `MemoryTestBase`. Never call memory functions in a test
  without that setUp, or you'll write into the real store.
- After `workspace_store(...)`, call `ensure_layout(store)` before writing
  directly to a bank file — the `memory-bank/` dir is not created until then.
- Use `self._ws` for the workspace path and `self.write_obs(text)` to seed
  observations.
- Keep tests fast and deterministic. No network, no sleeps longer than a few
  hundred ms, no reliance on wall-clock dates beyond what the code itself emits.

## Conventions

- One behavior per test; name it `test_<behavior>` and add a one-line docstring
  saying which bug it guards against.
- Import memory modules **inside** the test method (after `setUp` has set the env
  var) so module-level path resolution picks up `SILLY_MEMORY_HOME`.
- Prefer asserting on observable artifacts (bank files, `staging/pending.jsonl`,
  `events.jsonl`, the generated `_memory-context.mdc`) over internal state.
- When you add a new memory module or worker step, add at least one test for its
  failure mode (lock contention, empty input, malformed line, oversized input).

## Gotchas that have bitten us

- **Don't assert on docstring text.** Checking `"subprocess" not in source` once
  failed because a docstring said "spawns subprocesses". Assert on real usage
  (`"import subprocess"`, `"Popen"`, `"start_new_session"`).
- **Bank dir must exist** before `bank_path(...).write_text(...)` — call
  `ensure_layout(store)`.
- **`load_config` reads `SILLY_MEMORY_HOME/config.json`.** If routing/cap tests
  behave oddly, confirm `config.json` was copied into the temp home (setUp does
  this; don't remove it).
- **flock is per open-file-description.** Two `file_lock()` calls in the same
  process on the same path *do* conflict, which is why the timeout test works
  in-process.
- **You can't catch a hang with an assertion** — the test hangs too. For any
  loop/regex/string code that *could* run away, run it in a subprocess with
  `subprocess.run(..., timeout=N)` (see `TestNoRunaway.test_normalize_cannot_hang`).
  A real infinite loop raises `TimeoutExpired` → the test fails loudly instead of
  freezing the suite. Pair it with in-process assertions on bounded output size
  and elapsed time for the non-hanging (but still pathological) cases.

## What each test guards (bug history)

| Test | Bug it prevents |
|---|---|
| `TestDistiller.test_idempotent_staging_does_not_grow` | Quadratic `staging/pending.jsonl` growth → shell hangs / OOM ("big memory issue") |
| `TestDistiller.test_promotes_and_routes_to_correct_bank_file` | `_insert_bullet` `NameError` crash; wrong bank-file routing |
| `TestDistiller.test_no_duplicate_facts_in_bank` | Same fact promoted repeatedly |
| `TestDistiller.test_multitag_routes_by_first_tag` | Multi-tag lines routed by the last tag instead of the first |
| `TestRecall.test_special_characters_do_not_crash` | FTS5 `OperationalError` on `follow-up`, `t+2`, parens, quotes, empty query |
| `TestObserver.test_idempotent_no_duplicate_blocks` | Duplicate observation blocks on re-run |
| `TestObserver.test_activity_events_not_tracked_by_default` | file-edit/shell noise polluting `observations.md`; empty dated sections from noise-only batches |
| `TestObserver.test_user_prompts_always_tracked` | Regression that drops the high-signal user-prompt observations |
| `TestObserver.test_agent_responses_tracked_by_default` | Agent responses silently dropped when they should be recorded by default |
| `TestObserver.test_activity_tracked_when_enabled` | `observe_activity` opt-in flag not re-enabling file-edit/shell capture |
| `TestObserver.test_agent_responses_off_when_disabled` | `observe_agent_responses=false` not dropping agent replies; empty dated section from agent-only batch |
| `TestObserver.test_resume_after_marker_trimmed` | Observer stuck after event rotation drops its marker |
| `TestEventRotation.test_rotation_trims_and_keeps_marker` | `events.jsonl` growing unbounded; losing the resume marker |
| `TestContextPackCap.test_injected_pack_within_cap` | Injected `_memory-context.mdc` blowing past the token cap every turn |
| `TestRedaction.test_secrets_scrubbed` | Secrets (API keys, bearer tokens) leaking into memory |
| `TestHooksAreSafe.test_worker_never_spawns_subprocess` | Detached workers surviving Cursor restarts, holding locks (orphan-process bug) |
| `TestHooksAreSafe.test_command_hook_only_captures_no_processing` | Command hooks running the heavy pipeline synchronously and blocking the shell |
| `TestHooksAreSafe.test_process_queue_worker_guard` | Multiple workers piling up on the same store |
| `TestLocks.test_file_lock_timeout_raises` | Locks blocking forever instead of timing out |
| `TestLocks.test_append_event_fail_open_under_contention` | Event capture blocking the shell when a lock is held |
| `TestTasks.test_parse_open_done_and_fields` | `memory tasks` parser misreading status/title/owner/due/jira/tags |
| `TestTasks.test_render_filters` | `--status` / `--tag` / `--owner` filters returning wrong items |
| `TestTasks.test_none_fields_are_omitted` | Showing `jira: none` instead of omitting empty fields |
| `TestTasks.test_json_output_is_valid_and_filtered` | `memory tasks --json` emitting invalid JSON, wrong shape, or ignoring filters |
| `TestTasks.test_json_all_workspaces_shape` | `--json --all` losing the per-workspace grouping shape |
| `TestWorkspaces.test_lists_created_store_not_global` | `memory workspaces` missing stores or leaking `_global`/non-store dirs |
| `TestPaths.test_lists_bank_files_with_absolute_paths` | `memory paths` not emitting absolute paths or skipping derived files |
| `TestPaths.test_scope_all_includes_global_and_injected` | `--scope all` omitting global bank or the injected rule file |
| `TestNormalization.test_auto_skips_skill_only_entries` | Auto-distiller applying `[skill-only]` (common-word) mappings it shouldn't |
| `TestNormalization.test_no_map_is_noop` | Normalization breaking when no map file exists (e.g. shared installs) |
| `TestNormalization.test_malformed_variant_is_guarded` | A blank/1-char map entry producing runaway `\b..\b` replacement |
| `TestNormalization.test_distiller_applies_auto_normalization` | Distiller not canonicalizing names before writing to the bank |
| `TestNoRunaway.test_normalize_is_single_pass_no_growth_loop` | A `X→XY` (canonical contains variant) rule looping until OOM if changed to fixpoint/while-loop |
| `TestNoRunaway.test_normalize_chained_rules_terminate` | `cat→dog`, `dog→cat` style maps cycling forever |
| `TestNoRunaway.test_normalize_bounded_output_and_time` | Output size or runtime exploding on large inputs |
| `TestNoRunaway.test_normalize_cannot_hang` | **True hang-catcher** — adversarial map+input run in a subprocess with a hard 20s timeout |
| `TestNoRunaway.test_distill_with_normalization_stays_idempotent` | Normalization breaking the distiller's no-duplicate / no-staging-growth guarantee |
| `TestAutoGitignore.test_gitignore_created_for_new_workspace` | Memory artifacts getting committed to user repos |
| `TestZshHelpersMatchCli.test_every_helper_calls_a_registered_subcommand` | `memdelete`/`meminspect`/`memwhy`/… calling `mem delete`/`mem inspect`/… that argparse rejected |
| `TestZshHelpersMatchCli.test_memhelp_documents_only_defined_helpers` | `memhelp` advertising helpers (`memcompact-review`, `memcontradict`) with no CLI behind them |
| `TestSelftestInstalledLayout.test_installed_layout_imports_every_test_module` | `memtest` erroring on 5 perf modules in `~/.silly-memory/tests` (no `memory` package there) |
| `TestCursorAgentMemoryInjection.test_cursor_agent_memory_injection_e2e` | Injected memory not actually reaching a live `cursor-agent` session (opt-in) |
