"""Regression tests for silly-memory.

Stdlib-only (unittest). Each test runs against a throwaway SILLY_MEMORY_HOME so
the real store is never touched. Run with:

    python3 ~/.silly-memory/bin/memory selftest
    # or
    python3 -m unittest discover -s ~/.silly-memory/tests

Every test here pins a specific bug we hit during development so it can't regress:
  - distiller crash + unbounded staging growth (the "big memory issue")
  - recall crash on FTS special characters (follow-up, t+2)
  - observer dedup / resume after event rotation
  - context-pack token cap (injected every turn)
  - multi-tag routing, secret redaction
  - hooks must never spawn subprocesses or block (fail-open locks, worker guard)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEM_LIB))


class MemoryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_home_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        # The normalization map is cached per process; clear it so a map written
        # under one test's temp home can't leak into another test.
        try:
            from memory_system.system.normalize import clear_cache

            clear_cache()
        except Exception:
            pass
        # Copy the real config so bank routing/cap settings are present.
        real_cfg = MEM_HOME / "config.json"
        if real_cfg.exists():
            shutil.copy2(real_cfg, Path(self._tmp) / "config.json")
        self._ws = Path(tempfile.mkdtemp(prefix="memtest_ws_"))
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._ws, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)

    def write_obs(self, text: str) -> Path:
        from memory_system.paths import workspace_store

        store = workspace_store(self._ws)
        (store / "observations.md").write_text(text, encoding="utf-8")
        return store


class TestDistiller(MemoryTestBase):
    def test_idempotent_staging_does_not_grow(self) -> None:
        """The 'big memory issue': repeated distill must not re-stage the same lines."""
        from memory_system.recall.distiller import distill_from_observations

        store = self.write_obs(
            "# Observations\n\n"
            "- \U0001f7e1 [2026-06-05] #stakeholder: low-conf rambling text\n"
            "- \U0001f534 [2026-06-05] #action-item: do X; owner: Bob; due: TBD\n"
        )
        for _ in range(20):
            distill_from_observations(self._ws)
        staging = store / "staging" / "pending.jsonl"
        lines = [l for l in staging.read_text().splitlines() if l.strip()] if staging.exists() else []
        self.assertLessEqual(len(lines), 1, f"staging grew unbounded: {len(lines)} lines")

    def test_promotes_and_routes_to_correct_bank_file(self) -> None:
        """_insert_bullet must exist (the NameError crash) and route by category."""
        from memory_system.recall.distiller import distill_from_observations
        from memory_system.paths import bank_path, workspace_store

        store = self.write_obs(
            "# Observations\n\n"
            "- \U0001f534 [2026-06-05] #action-item: ship catalog; owner: Ana; due: 2026-07-01\n"
            "- \U0001f7e1 [2026-06-05] #decision: defer limit orders; owner: Lee; due: Q4\n"
        )
        distill_from_observations(self._ws)
        actions = bank_path(workspace_store(self._ws), "actionItems.md").read_text()
        domain = bank_path(workspace_store(self._ws), "domainContext.md").read_text()
        self.assertIn("ship catalog", actions)
        self.assertIn("defer limit orders", domain)

    def test_no_duplicate_facts_in_bank(self) -> None:
        from memory_system.recall.distiller import distill_from_observations
        from memory_system.paths import bank_path, workspace_store

        self.write_obs(
            "# Observations\n\n"
            "- \U0001f534 [2026-06-05] #action-item: unique task; owner: Sam; due: TBD\n"
        )
        for _ in range(5):
            distill_from_observations(self._ws)
        actions = bank_path(workspace_store(self._ws), "actionItems.md").read_text()
        self.assertEqual(actions.count("unique task"), 1)

    def test_multitag_routes_by_first_tag(self) -> None:
        from memory_system.recall.distiller import _parse_observation

        parsed = _parse_observation("- \U0001f7e1 [2026-06-05] #stakeholder #payments #be: Alex is BE")
        self.assertEqual(parsed["category"], "stakeholder")


class TestRecall(MemoryTestBase):
    def test_special_characters_do_not_crash(self) -> None:
        """recall on 'follow-up', 't+2', parens, quotes must not raise FTS errors."""
        from memory_system.index import rebuild_index
        from memory_system.paths import bank_path, ensure_layout, workspace_store
        from memory_system.index import recall_all

        store = workspace_store(self._ws)
        ensure_layout(store)
        bank_path(store, "domainContext.md").write_text(
            "# Domain\n\n- [2026-06-05] #decision: follow-up on t+2 release (beta) \"x\"\n"
        )
        rebuild_index(store, store.name)
        for q in ["follow-up", "t+2 release", "release (beta)", 'a "quote"', "-leading", ""]:
            try:
                recall_all(self._ws, q)
            except Exception as e:  # noqa: BLE001
                self.fail(f"recall crashed on {q!r}: {e}")


class TestObserver(MemoryTestBase):
    def test_idempotent_no_duplicate_blocks(self) -> None:
        from memory_system.events import append_event
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        for i in range(3):
            append_event(self._ws, "beforeSubmitPrompt", {"prompt": f"m{i}"})
        store = workspace_store(self._ws)
        run_observer(self._ws, force=True)
        run_observer(self._ws, force=True)  # no new events -> must not append
        blocks = (store / "observations.md").read_text().count("\n## ")
        self.assertLessEqual(blocks, 1)

    def _set_cfg(self, **kv) -> None:
        import json as _json

        from memory_system.system.config import memory_home

        cfg_path = memory_home() / "config.json"
        cfg = _json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        cfg.update(kv)
        cfg_path.write_text(_json.dumps(cfg), encoding="utf-8")

    def test_activity_events_not_tracked_by_default(self) -> None:
        """file-edit/shell are noise: must not land in observations.md by default,
        and an activity-only batch must not write an empty dated section."""
        from memory_system.events import append_event
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        append_event(self._ws, "afterFileEdit", {"file_path": "/tmp/foo.py"})
        append_event(self._ws, "afterShellExecution", {"command": "ls -la"})
        store = workspace_store(self._ws)
        self.assertTrue(run_observer(self._ws, force=True))  # marker still advances
        obs = (store / "observations.md").read_text()
        self.assertNotIn("#file-edit", obs)
        self.assertNotIn("#shell", obs)
        self.assertEqual(obs.count("\n## "), 0)  # no empty dated block

    def test_user_prompts_always_tracked(self) -> None:
        """User prompts are the high-signal source — always recorded."""
        from memory_system.events import append_event
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        append_event(self._ws, "beforeSubmitPrompt", {"prompt": "Alex is a BE engineer"})
        store = workspace_store(self._ws)
        run_observer(self._ws, force=True)
        self.assertIn("User prompt snippet", (store / "observations.md").read_text())

    def test_agent_responses_tracked_by_default(self) -> None:
        """Agent responses are recorded by default (observe_agent_responses=true)."""
        from memory_system.events import append_event
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        append_event(self._ws, "afterAgentResponse", {"text": "Here is the summary."})
        store = workspace_store(self._ws)
        run_observer(self._ws, force=True)
        self.assertIn("Agent response", (store / "observations.md").read_text())

    def test_activity_tracked_when_enabled(self) -> None:
        """With observe_activity=true, file-edit/shell are recorded (opt-in)."""
        from memory_system.events import append_event
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        self._set_cfg(observe_activity=True)
        append_event(self._ws, "afterFileEdit", {"file_path": "/tmp/foo.py"})
        store = workspace_store(self._ws)
        run_observer(self._ws, force=True)
        self.assertIn("#file-edit", (store / "observations.md").read_text())

    def test_agent_responses_off_when_disabled(self) -> None:
        """observe_agent_responses=false drops agent replies; an agent-only batch
        must not write an empty dated section."""
        from memory_system.events import append_event
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        self._set_cfg(observe_agent_responses=False)
        append_event(self._ws, "afterAgentResponse", {"text": "Sure, I decided to do X."})
        store = workspace_store(self._ws)
        self.assertTrue(run_observer(self._ws, force=True))  # marker still advances
        obs = (store / "observations.md").read_text()
        self.assertNotIn("Agent response", obs)
        self.assertEqual(obs.count("\n## "), 0)

    def test_resume_after_marker_trimmed(self) -> None:
        """Event rotation can drop the marker; observer must not get stuck."""
        from memory_system.events import append_event, rotate_events
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        for i in range(8):
            append_event(self._ws, "beforeSubmitPrompt", {"prompt": f"m{i}"})
        store = workspace_store(self._ws)
        run_observer(self._ws, force=True)
        last = json.loads((store / ".observer_state.json").read_text())["last_event_id"]
        rotate_events(store, last, max_lines=2)
        for i in range(3):
            append_event(self._ws, "beforeSubmitPrompt", {"prompt": f"new{i}"})
        self.assertTrue(run_observer(self._ws, force=True))
        self.assertIn("new2", (store / "observations.md").read_text())


class TestEventRotation(MemoryTestBase):
    def test_rotation_trims_and_keeps_marker(self) -> None:
        from memory_system.events import append_event, rotate_events
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        for i in range(30):
            append_event(self._ws, "beforeSubmitPrompt", {"prompt": f"m{i}"})
        store = workspace_store(self._ws)
        run_observer(self._ws, force=True)
        last = json.loads((store / ".observer_state.json").read_text())["last_event_id"]
        before = len((store / "events.jsonl").read_text().splitlines())
        dropped = rotate_events(store, last, max_lines=5)
        after = len((store / "events.jsonl").read_text().splitlines())
        self.assertGreater(dropped, 0)
        self.assertLess(after, before)
        # marker line must survive so the observer can resume
        ids = [json.loads(l)["id"] for l in (store / "events.jsonl").read_text().splitlines() if l.strip()]
        self.assertIn(last, ids)


class TestContextPackCap(MemoryTestBase):
    def test_injected_pack_within_cap(self) -> None:
        """Pack is injected every turn; it must stay within the token cap."""
        from memory_system.system.config import load_config
        from memory_system.recall.context_pack import render_rule_file
        from memory_system.paths import bank_path, ensure_layout, generated_rule_path, workspace_store

        store = workspace_store(self._ws)
        ensure_layout(store)
        (self._ws / "meetingNotes").mkdir(exist_ok=True)
        (self._ws / "meetingNotes" / "README.md").write_text("# notes\n")
        huge = "# Domain\n\n" + "".join(
            f"- [2026-06-05] #decision: fact {i} " + "x" * 100 + "\n" for i in range(4000)
        )
        bank_path(store, "domainContext.md").write_text(huge)
        render_rule_file(self._ws)
        rule = generated_rule_path(self._ws)
        chars = len(rule.read_text())
        cap_chars = int(load_config().get("context_pack_token_cap", 8000)) * 4
        self.assertLessEqual(chars, cap_chars, f"pack {chars} exceeds cap {cap_chars}")
        self.assertIn("memory recall", rule.read_text())  # footer survives trim


class TestRedaction(MemoryTestBase):
    def test_secrets_scrubbed(self) -> None:
        from memory_system.redact import sanitize_payload

        out = sanitize_payload(
            {"command": "export API_KEY=sk-EXAMPLE0000000000000000000000000000000000000000", "output": "Bearer EXAMPLE_TOKEN"}
        )
        blob = json.dumps(out)
        self.assertNotIn("sk-EXAMPLE0000000000000000000000000000000000000000", blob)
        self.assertIn("REDACTED", blob)


class TestHooksAreSafe(MemoryTestBase):
    def test_worker_never_spawns_subprocess(self) -> None:
        """The orphan-process bug: hooks must not spawn detached workers."""
        import memory_system.events.worker as worker

        self.assertFalse(
            hasattr(worker, "spawn_background_process"),
            "spawn_background_process must not exist (caused restart-surviving orphans)",
        )
        src = Path(worker.__file__).read_text()
        self.assertNotIn("import subprocess", src, "worker.py must not import subprocess")
        self.assertNotIn("Popen", src, "worker.py must not use Popen")
        self.assertNotIn("start_new_session", src)

    def test_command_hook_only_captures_no_processing(self) -> None:
        """afterShellExecution must NOT run the heavy pipeline (kept the shell fast)."""
        from memory_system.events.worker import handle_hook_job
        from memory_system.paths import workspace_store

        store = workspace_store(self._ws)
        handle_hook_job(self._ws, "afterShellExecution")
        # observations must be untouched by a command hook (no observe ran)
        obs = (store / "observations.md").read_text()
        self.assertNotIn("\n## ", obs)
        # but the job must be enqueued for later
        queue = store / "queues" / "pending.jsonl"
        self.assertTrue(queue.exists() and queue.read_text().strip())

    def test_process_queue_worker_guard(self) -> None:
        """Only one worker at a time: if the worker lock is held, return fast."""
        from memory_system.safety import file_lock
        from memory_system.events.worker import _worker_lock, process_queue
        from memory_system.paths import workspace_store

        store = workspace_store(self._ws)
        with file_lock(_worker_lock(store)):  # simulate another worker holding it
            process_queue(self._ws)  # must return immediately, not block


class TestLocks(MemoryTestBase):
    def test_file_lock_timeout_raises(self) -> None:
        from memory_system.safety import LockTimeout, file_lock

        lockf = self._ws / ".lock"
        with file_lock(lockf):
            with self.assertRaises(LockTimeout):
                with file_lock(lockf, timeout=0.2):
                    pass

    def test_append_event_fail_open_under_contention(self) -> None:
        """Even if the lock is held, capture must not block (fail-open)."""
        from memory_system.events import append_event
        from memory_system.paths import lock_path, workspace_store
        from memory_system.safety import file_lock

        store = workspace_store(self._ws)
        with file_lock(lock_path(store)):  # hold the store lock
            eid = append_event(self._ws, "afterShellExecution", {"command": "ls"})
        self.assertTrue(eid)
        self.assertTrue((store / "events.jsonl").read_text().strip())


class TestTasks(MemoryTestBase):
    def _seed(self) -> None:
        from memory_system.paths import bank_path, ensure_layout, workspace_store

        store = workspace_store(self._ws)
        ensure_layout(store)
        bank_path(store, "actionItems.md").write_text(
            "# Action items\n\n## Open\n\n"
            "- [ ] [2026-06-04] #action #release: Ship catalog — owner: Ana; due: 2026-07-01; jira: DEMO-1; source: `x.md`.\n"
            "- [ ] [2026-06-02] #action #billing: Mock ACME — owner: Bob; due: TBD; jira: none.\n\n"
            "## Done\n\n"
            "- [x] [2026-05-21] #action #release: Old task — owner: Ana; due: TBD; done: 2026-05-27.\n"
        )

    def test_parse_open_done_and_fields(self) -> None:
        from memory_system.status import _parse_action_items
        from memory_system.paths import bank_path, workspace_store

        self._seed()
        items = _parse_action_items(bank_path(workspace_store(self._ws), "actionItems.md"))
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertFalse(first["done"])
        self.assertEqual(first["title"], "Ship catalog")
        self.assertEqual(first["owner"], "Ana")
        self.assertEqual(first["due"], "2026-07-01")
        self.assertEqual(first["jira"], "DEMO-1")
        self.assertIn("release", first["tags"])
        self.assertNotIn("action", first["tags"])  # generic #action tag dropped

    def test_render_filters(self) -> None:
        from memory_system.status import render_tasks

        self._seed()
        open_only = render_tasks(self._ws, status="open")
        self.assertIn("Ship catalog", open_only)
        self.assertNotIn("Old task", open_only)
        done_only = render_tasks(self._ws, status="done")
        self.assertIn("Old task", done_only)
        self.assertNotIn("Ship catalog", done_only)
        by_tag = render_tasks(self._ws, status="all", tag="billing")
        self.assertIn("Mock ACME", by_tag)
        self.assertNotIn("Ship catalog", by_tag)
        by_owner = render_tasks(self._ws, status="all", owner="bob")
        self.assertIn("Mock ACME", by_owner)
        self.assertNotIn("Ship catalog", by_owner)

    def test_none_fields_are_omitted(self) -> None:
        from memory_system.status import _parse_action_items
        from memory_system.paths import bank_path, workspace_store

        self._seed()
        items = _parse_action_items(bank_path(workspace_store(self._ws), "actionItems.md"))
        self.assertIsNone(items[1]["jira"])  # "jira: none" -> None

    def test_json_output_is_valid_and_filtered(self) -> None:
        import json as _json

        from memory_system.status import render_tasks_json

        self._seed()
        data = _json.loads(render_tasks_json(self._ws, status="open"))
        self.assertEqual(data["scope"], "workspace")
        self.assertEqual(data["filters"]["status"], "open")
        self.assertEqual(data["total"], 2)
        titles = [t["title"] for t in data["items"]]
        self.assertIn("Ship catalog", titles)
        self.assertNotIn("Old task", titles)  # done item excluded
        first = data["items"][0]
        self.assertEqual(first["owner"], "Ana")
        self.assertEqual(first["jira"], "DEMO-1")
        self.assertFalse(first["done"])

    def test_json_all_workspaces_shape(self) -> None:
        import json as _json

        from memory_system.status import render_tasks_json

        self._seed()
        data = _json.loads(render_tasks_json(self._ws, status="all", all_workspaces=True))
        self.assertEqual(data["scope"], "all")
        self.assertIn("workspaces", data)
        self.assertGreaterEqual(data["total"], 3)
        self.assertTrue(all("items" in g and "workspace" in g for g in data["workspaces"]))


class TestWorkspaces(MemoryTestBase):
    def test_lists_created_store_not_global(self) -> None:
        from memory_system.status import iter_workspaces, render_workspaces
        from memory_system.paths import workspace_store

        workspace_store(self._ws)  # registers a .meta.json
        rows = iter_workspaces()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["root"], str(self._ws.resolve()))
        out = render_workspaces()
        self.assertIn(str(self._ws.resolve()), out)
        self.assertNotIn("_global", out)


class TestPaths(MemoryTestBase):
    def test_lists_bank_files_with_absolute_paths(self) -> None:
        from memory_system.status import render_paths
        from memory_system.paths import bank_path, ensure_layout, workspace_store

        store = workspace_store(self._ws)
        ensure_layout(store)
        bank_path(store, "actionItems.md").write_text("# Action items\n\n- [ ] [2026-06-05] #action: x\n")
        out = render_paths(self._ws, scope="workspace")
        # absolute path to the bank file must appear
        self.assertIn(str(bank_path(store, "actionItems.md")), out)
        # derived files created by ensure_layout must be listed
        self.assertIn(str(store / "observations.md"), out)
        # global section excluded when scope=workspace
        self.assertNotIn("[global]", out)

    def test_scope_all_includes_global_and_injected(self) -> None:
        from memory_system.status import render_paths
        from memory_system.paths import ensure_layout, global_store, workspace_store

        ensure_layout(workspace_store(self._ws))
        ensure_layout(global_store())
        out = render_paths(self._ws, scope="all")
        self.assertIn("[workspace]", out)
        self.assertIn("[global]", out)
        self.assertIn("[injected]", out)


class TestNormalization(MemoryTestBase):
    def _write_map(self, text: str) -> None:
        from memory_system.system.normalize import clear_cache

        (Path(self._tmp) / "name-normalization.md").write_text(text, encoding="utf-8")
        clear_cache()

    def test_auto_skips_skill_only_entries(self) -> None:
        from memory_system.system.normalize import normalize_text

        self._write_map("- `up West` -> `Upvest`\n- `Reason` -> `Raisin` [skill-only]\n")
        self.assertEqual(normalize_text("met up West about Reason"), "met Upvest about Reason")
        self.assertEqual(
            normalize_text("met up West about Reason", include_skill_only=True),
            "met Upvest about Raisin",
        )

    def test_no_map_is_noop(self) -> None:
        from memory_system.system.normalize import clear_cache, normalize_text

        clear_cache()
        self.assertEqual(normalize_text("up West"), "up West")

    def test_malformed_variant_is_guarded(self) -> None:
        from memory_system.system.normalize import normalize_text

        # blank + 1-char variants must be ignored (no \b..\b that matches everywhere)
        self._write_map("- `` -> `X`\n- `a` -> `Y`\n- `Saga` -> `Sergei`\n")
        self.assertEqual(normalize_text("Saga a"), "Sergei a")

    def test_distiller_applies_auto_normalization(self) -> None:
        from memory_system.recall.distiller import distill_from_observations
        from memory_system.paths import bank_path, workspace_store

        self._write_map("- `up West` -> `Upvest`\n")
        self.write_obs(
            "# Observations\n\n"
            "- \U0001f534 [2026-06-05] #decision: migrate to up West; owner: Ana; due: TBD\n"
        )
        distill_from_observations(self._ws)
        domain = bank_path(workspace_store(self._ws), "domainContext.md").read_text()
        self.assertIn("Upvest", domain)
        self.assertNotIn("up West", domain)


class TestNoRunaway(MemoryTestBase):
    """Guard against infinite loops / text explosion / unbounded memory growth."""

    def _write_map(self, text: str) -> None:
        from memory_system.system.normalize import clear_cache

        (Path(self._tmp) / "name-normalization.md").write_text(text, encoding="utf-8")
        clear_cache()

    def test_normalize_is_single_pass_no_growth_loop(self) -> None:
        # canonical CONTAINS the variant — a loop-until-stable impl would blow up
        # ("X"->"XY"->"XYY"->...). Single-pass must give exactly one rewrite.
        from memory_system.system.normalize import normalize_text

        self._write_map("- `Xy` -> `XyZ`\n")
        self.assertEqual(normalize_text("Xy Xy"), "XyZ XyZ")

    def test_normalize_chained_rules_terminate(self) -> None:
        from memory_system.system.normalize import normalize_text

        # cat->dog then dog->cat: sequential single pass, must terminate.
        self._write_map("- `cat` -> `dog`\n- `dog` -> `cat`\n")
        self.assertEqual(normalize_text("cat dog"), "cat cat")

    def test_normalize_bounded_output_and_time(self) -> None:
        import time

        from memory_system.system.normalize import normalize_text

        self._write_map("- `Saga` -> `Sergei`\n")
        big = "Saga " * 5000
        start = time.time()
        out = normalize_text(big)
        elapsed = time.time() - start
        self.assertLess(elapsed, 2.0, f"normalize too slow: {elapsed:.2f}s")
        self.assertLess(len(out), len(big) * 3, "output exploded")
        self.assertNotIn("Saga", out)

    def test_normalize_cannot_hang(self) -> None:
        # True hang-catcher: run in a subprocess with a hard timeout. An infinite
        # loop here raises TimeoutExpired -> the test fails instead of hanging.
        self._write_map("- `a1` -> `a1x`\n- `x` -> `xx`\n")
        code = (
            f"import sys; sys.path.insert(0, {str(MEM_LIB)!r});"
            "from memory_system.system.normalize import normalize_text;"
            "print(len(normalize_text('a1 ' * 2000 + 'x ' * 2000)))"
        )
        env = {**os.environ, "SILLY_MEMORY_HOME": self._tmp}
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=20
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_distill_with_normalization_stays_idempotent(self) -> None:
        # Normalization must not break the distiller's no-growth guarantee.
        from memory_system.recall.distiller import distill_from_observations
        from memory_system.paths import bank_path, workspace_store

        self._write_map("- `up West` -> `Upvest`\n")
        store = self.write_obs(
            "# Observations\n\n"
            "- \U0001f534 [2026-06-05] #decision: pick up West; owner: Ana; due: TBD\n"
        )
        for _ in range(25):
            distill_from_observations(self._ws)
        domain = bank_path(workspace_store(self._ws), "domainContext.md").read_text()
        self.assertEqual(domain.count("Upvest"), 1, "duplicate facts accumulated")
        staging = store / "staging" / "pending.jsonl"
        lines = [l for l in staging.read_text().splitlines() if l.strip()] if staging.exists() else []
        self.assertLessEqual(len(lines), 1, f"staging grew unbounded: {len(lines)}")


class TestAutoGitignore(MemoryTestBase):
    def test_gitignore_created_for_new_workspace(self) -> None:
        from memory_system.recall.context_pack import render_rule_file

        render_rule_file(self._ws)
        cursor_ignore = (self._ws / ".cursor" / ".gitignore").read_text().splitlines()
        self.assertIn("rules/_memory-context.mdc", cursor_ignore)
        # The marker lives in .silly-memory/, which ignores everything.
        self.assertIn("*", (self._ws / ".silly-memory" / ".gitignore").read_text().splitlines())


if __name__ == "__main__":
    unittest.main(verbosity=2)
