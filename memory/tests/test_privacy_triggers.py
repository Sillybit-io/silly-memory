"""``<private>`` spans and explicit "remember that …" facts.

Guards:
  - ``<private>`` spans (closed, or unclosed through the end) are stripped
    before the secret patterns, so nothing inside one reaches events, banks,
    or the AI-text log; secrets outside a span are still redacted.
  - "remember that …" fires only when it opens the prompt, and stores one
    full-confidence ``explicit`` fact, deduplicated against the target bank
    and appended at the end so existing ``filename:line`` ids stay valid.
  - The observer stores explicit prompts even in a short session below the
    default 6,000-token threshold, without moving the passive cursor, and a
    failed write stays retryable.

Isolation: every test uses a throwaway memory home and project.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
CLI = MEM_HOME / "bin" / "memory"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system import index  # noqa: E402
from memory_system.events import append_event  # noqa: E402
from memory_system.events import observer  # noqa: E402
from memory_system.learning import explicit  # noqa: E402
from memory_system.learning.classifier import reset_classifier_for_tests  # noqa: E402
from memory_system.learning.explicit import extract_explicit_fact, store_explicit_fact  # noqa: E402
from memory_system.lifecycle.scoring import bump_access, load_scores  # noqa: E402
from memory_system.paths import global_store, lock_path, workspace_store  # noqa: E402
from memory_system.recall.recall_hybrid import _load_bank_entries  # noqa: E402
from memory_system.redact import redact_text, sanitize_payload  # noqa: E402
from memory_system.safety import LockTimeout, file_lock  # noqa: E402

_HOME_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN")
FACT = "we deploy only from main"


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_privacy_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _HOME_VARS}
        self.home = self.tmp / "home"
        self.home.mkdir()
        shutil.copy2(MEM_HOME / "config.json", self.home / "config.json")
        os.environ["SILLY_MEMORY_HOME"] = str(self.home)
        self.project = self.tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        reset_classifier_for_tests()

    def tearDown(self) -> None:
        reset_classifier_for_tests()
        for key in _HOME_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bank(self, name: str = "domainContext.md", store: Path | None = None) -> Path:
        return (store or workspace_store(self.project)) / "memory-bank" / name

    def explicit_lines(self, bank_file: Path) -> list[tuple[int, str]]:
        if not bank_file.exists():
            return []
        return [
            (n, line)
            for n, line in enumerate(bank_file.read_text(encoding="utf-8").splitlines(), start=1)
            if "#explicit" in line
        ]

    def prompt_event(self, text: str, conversation: str = "c1") -> str:
        return append_event(
            self.project,
            "beforeSubmitPrompt",
            {"hook_event_name": "beforeSubmitPrompt", "workspace_roots": [str(self.project)], "conversation_id": conversation, "prompt": text},
        )


class TestPrivateSpans(unittest.TestCase):
    def test_private_span_is_stripped_before_secret_patterns(self) -> None:
        self.assertEqual(redact_text("plan <private>token=abc</private> done"), "plan [private] done")
        self.assertEqual(
            redact_text("a <Private>one</PRIVATE> b <private>two\nlines</private> c"),
            "a [private] b [private] c",
        )

    def test_unclosed_private_tag_strips_to_end(self) -> None:
        self.assertEqual(redact_text("keep this <PRIVATE>secret\nand more"), "keep this [private]")

    def test_secret_outside_private_span_is_still_redacted(self) -> None:
        self.assertEqual(redact_text("token=abc <private>x</private>"), "token=[REDACTED] [private]")
        self.assertIn("[REDACTED]", redact_text("key sk-" + "a" * 24 + " <private>x</private>"))

    def test_sanitize_payload_strips_private_prompt_and_reply_text(self) -> None:
        clean = sanitize_payload({"prompt": "use <private>hunter2</private> now", "text": "ok <private>hunter2"})
        self.assertNotIn("hunter2", json.dumps(clean))


class TestExtractExplicitFact(unittest.TestCase):
    def test_triggers_only_when_the_imperative_opens_the_prompt(self) -> None:
        cases = {
            "remember that we deploy only from main": FACT,
            "  Remember that we deploy only from main": FACT,
            "please remember that Maya owns billing": "Maya owns billing",
            "Remember: tabs over spaces\nnow fix the tests": "tabs over spaces",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(extract_explicit_fact(prompt), expected)

    def test_remember_that_mid_sentence_is_ignored(self) -> None:
        for prompt in (
            "can you remember that we deploy only from main",
            "I will remember that",
            "remember that time we broke prod",
            "Remember that one time in June",
            "remembering is hard",
            "remember that.",
            "remember that <private>the password</private>",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(extract_explicit_fact(prompt))

    def test_a_prompt_wrapped_in_quotes_still_triggers(self) -> None:
        """`opencode run "remember that ..."` records the message inside quotes, inner quotes escaped."""
        cases = {
            '"remember that we deploy only from main"': "we deploy only from main",
            '"remember that the flag is \\"beta\\""': 'the flag is "beta"',
            "'remember that reviews need two approvals'": "reviews need two approvals",
            "“remember that the on-call channel is #ops”": "the on-call channel is #ops",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(extract_explicit_fact(prompt), expected)
        for prompt in ('"can you remember that we deploy from main"', '"remember that we deploy from main', '"remember that time"'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(extract_explicit_fact(prompt))


class TestStoreExplicitFact(_Isolated):
    def test_remember_that_at_prompt_start_stores_explicit_fact(self) -> None:
        fact = extract_explicit_fact("remember that we deploy only from main")
        assert fact is not None
        bank_file = store_explicit_fact(self.project, fact)
        self.assertEqual(bank_file, self.bank())
        lines = self.explicit_lines(self.bank())
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0][1].endswith(f"#explicit: {FACT}"), lines)

    def test_explicit_fact_has_confidence_one_and_tag(self) -> None:
        store_explicit_fact(self.project, FACT)
        (lineno, _line), = self.explicit_lines(self.bank())
        entry_id = f"domainContext.md:{lineno}"
        entry = load_scores(self.bank())[entry_id]
        self.assertEqual(entry["score"], 1.0)
        self.assertEqual(entry["tags"], ["explicit"])
        # The same physical id is the one hybrid recall reads.
        self.assertIn(entry_id, _load_bank_entries(workspace_store(self.project)))

    def test_same_fact_is_stored_once_and_metadata_is_kept(self) -> None:
        store_explicit_fact(self.project, FACT)
        (lineno, _), = self.explicit_lines(self.bank())
        bump_access(self.bank(), f"domainContext.md:{lineno}")
        store_explicit_fact(self.project, "We deploy   only from MAIN.")
        self.assertEqual(len(self.explicit_lines(self.bank())), 1)
        entry = load_scores(self.bank())[f"domainContext.md:{lineno}"]
        self.assertEqual(entry["access_count"], 1)
        self.assertEqual(entry["score"], 1.0)

    def test_existing_distilled_bullet_is_repaired_not_duplicated(self) -> None:
        bank_file = self.bank()
        bank_file.parent.mkdir(parents=True, exist_ok=True)
        bank_file.write_text(
            "# Domain\n\n- [2026-01-01] #decision: We deploy only from main; source: session-2026-01-01\n",
            encoding="utf-8",
        )
        bump_access(bank_file, "domainContext.md:3")
        store_explicit_fact(self.project, FACT)
        self.assertEqual(len(bank_file.read_text(encoding="utf-8").splitlines()), 3)
        entry = load_scores(bank_file)["domainContext.md:3"]
        self.assertEqual((entry["score"], entry["tags"], entry["access_count"]), (1.0, ["explicit"], 1))

    def test_new_fact_is_appended_so_existing_ids_stay_valid(self) -> None:
        bank_file = self.bank()
        bank_file.parent.mkdir(parents=True, exist_ok=True)
        before = "# Domain\n\n- first fact\n\n## Later\n- second fact\n\n\n"
        bank_file.write_text(before, encoding="utf-8")
        ids_before = set(_load_bank_entries(workspace_store(self.project)))
        store_explicit_fact(self.project, FACT)
        text = bank_file.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(before.rstrip("\n") + "\n"))
        after = _load_bank_entries(workspace_store(self.project))
        self.assertTrue(ids_before < set(after))
        self.assertEqual(after["domainContext.md:7"]["snippet"], f"[{explicit.date.today().isoformat()}] #explicit: {FACT}")

    def test_scope_routes_to_global_or_workspace(self) -> None:
        learned = self.bank("learned-memories.md", global_store())
        self.assertEqual(store_explicit_fact(self.project, "I always prefer tabs over spaces"), learned)
        self.assertEqual(store_explicit_fact(self.project, "the staging url is internal only", scope="global"), learned)
        self.assertEqual(
            store_explicit_fact(self.project, "I always prefer small pull requests", scope="workspace"),
            self.bank(),
        )
        with self.assertRaises(ValueError):
            store_explicit_fact(self.project, FACT, scope="everywhere")

    def test_empty_or_private_only_fact_is_a_no_op(self) -> None:
        for text in ("", "   ", "<private>only this</private>", "<private>unclosed secret"):
            with self.subTest(text=text):
                self.assertIsNone(store_explicit_fact(self.project, text))
        self.assertFalse(self.bank().exists())

    def test_private_text_never_reaches_the_bank(self) -> None:
        store_explicit_fact(self.project, "the deploy key is <private>sk-hunter2</private> in the vault")
        self.assertNotIn("hunter2", self.bank().read_text(encoding="utf-8"))
        self.assertIn("the deploy key is [private] in the vault", self.bank().read_text(encoding="utf-8"))

    def test_locked_store_times_out_without_writing(self) -> None:
        store = workspace_store(self.project)
        with mock.patch.object(explicit, "STORE_LOCK_TIMEOUT", 0.2), file_lock(lock_path(store), timeout=1):
            with self.assertRaises(LockTimeout):
                store_explicit_fact(self.project, FACT)
        self.assertFalse(self.bank().exists())

    def test_index_is_rebuilt_after_the_locks_are_released(self) -> None:
        done = threading.Event()

        def run() -> None:
            store_explicit_fact(self.project, FACT)
            done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=30)
        self.assertTrue(done.is_set(), "store_explicit_fact deadlocked on its own store lock")
        hits = index.recall(workspace_store(self.project), "deploy main")
        self.assertTrue(any("deploy" in str(hit.get("snippet", "")) for hit in hits), hits)

    def test_concurrent_writers_store_one_bullet(self) -> None:
        workspace_store(self.project)
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from memory_system.learning.explicit import store_explicit_fact\n"
            "from memory_system.events.observer import run_observer\n"
            "root = Path(sys.argv[2])\n"
            "if sys.argv[1] == 'observer':\n"
            "    run_observer(root)\n"
            "else:\n"
            f"    store_explicit_fact(root, {FACT!r})\n"
        )
        self.prompt_event(f"remember that {FACT}")
        env = {k: v for k, v in os.environ.items() if k not in _HOME_VARS}
        env.update({"SILLY_MEMORY_HOME": str(self.home), "PYTHONPATH": str(MEM_LIB), "PYTHONDONTWRITEBYTECODE": "1"})
        procs = [
            subprocess.Popen([sys.executable, "-c", script, role, str(self.project)], env=env, stderr=subprocess.PIPE, text=True)
            for role in ("observer", "direct", "observer", "direct")
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
        lines = self.explicit_lines(self.bank())
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(load_scores(self.bank())[f"domainContext.md:{lines[0][0]}"]["score"], 1.0)


class TestObserverExplicitPass(_Isolated):
    def observer_state(self) -> Path:
        return workspace_store(self.project) / ".observer_state.json"

    def test_short_session_stores_fact_below_the_default_threshold(self) -> None:
        self.prompt_event(f"remember that {FACT}")
        self.assertTrue(observer.run_observer(self.project))
        self.assertEqual(len(self.explicit_lines(self.bank())), 1)
        self.assertFalse(self.observer_state().exists(), "the passive cursor must not move below the threshold")
        # Replays while passive work stays deferred do not duplicate the fact.
        observer.run_observer(self.project)
        store_explicit_fact(self.project, FACT)
        self.assertEqual(len(self.explicit_lines(self.bank())), 1)

    def test_explicit_prompts_across_a_51_event_batch(self) -> None:
        first = self.prompt_event(f"remember that {FACT}")
        for n in range(49):
            self.prompt_event(f"look at module number {n}")
        last = self.prompt_event("remember that the release train leaves on Tuesdays")
        self.assertTrue(observer.run_observer(self.project, force=True))
        texts = [line for _, line in self.explicit_lines(self.bank())]
        self.assertEqual(len(texts), 2, texts)
        observations = (workspace_store(self.project) / "observations.md").read_text(encoding="utf-8")
        self.assertIn("look at module number 48", observations)
        self.assertNotIn("User prompt snippet — remember that", observations)
        state = json.loads(self.observer_state().read_text(encoding="utf-8"))
        self.assertEqual(state["last_event_id"], last)
        self.assertNotEqual(first, last)

    def test_failed_explicit_write_keeps_the_cursor_retryable(self) -> None:
        self.prompt_event(f"remember that {FACT}")
        self.prompt_event("look at the parser")
        with mock.patch.object(observer, "store_explicit_fact", side_effect=LockTimeout("busy")):
            self.assertFalse(observer.run_observer(self.project, force=True))
        self.assertFalse(self.observer_state().exists())
        self.assertFalse(self.bank().exists())
        self.assertTrue(observer.run_observer(self.project, force=True))
        self.assertEqual(len(self.explicit_lines(self.bank())), 1)
        self.assertTrue(self.observer_state().exists())


class TestHookLifecycle(_Isolated):
    def hook(self, payload: dict) -> str:
        env = {k: v for k, v in os.environ.items() if k not in _HOME_VARS}
        env.update({"SILLY_MEMORY_HOME": str(self.home), "HOME": str(self.tmp), "MEMORY_EMBEDDING_BACKEND": "noop", "PYTHONDONTWRITEBYTECODE": "1"})
        proc = subprocess.run(
            [sys.executable, str(CLI), "hook"],
            input=json.dumps({"workspace_roots": [str(self.project)], "conversation_id": "c1", **payload}),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_default_threshold_session_end_stores_one_explicit_fact(self) -> None:
        self.hook({"hook_event_name": "sessionStart"})
        self.hook({"hook_event_name": "beforeSubmitPrompt", "prompt": f"remember that {FACT}"})
        self.hook({"hook_event_name": "sessionEnd"})
        lines = self.explicit_lines(self.bank())
        self.assertEqual(len(lines), 1, lines)
        entry = load_scores(self.bank())[f"domainContext.md:{lines[0][0]}"]
        self.assertEqual((entry["score"], entry["tags"]), (1.0, ["explicit"]))
        self.hook({"hook_event_name": "sessionEnd"})
        store_explicit_fact(self.project, FACT)
        self.assertEqual(len(self.explicit_lines(self.bank())), 1)

    def test_private_spans_never_reach_any_stored_file(self) -> None:
        self.hook({"hook_event_name": "beforeSubmitPrompt", "prompt": "remember that the key is <private>hunter2-prompt</private> in vault"})
        self.hook({"hook_event_name": "afterAgentResponse", "text": "noted <private>hunter2-reply"})
        self.hook({"hook_event_name": "sessionEnd"})
        self.assertEqual(len(self.explicit_lines(self.bank())), 1)
        scanned = 0
        for root in (self.home, self.project):
            for path in root.rglob("*"):
                if path.is_file():
                    scanned += 1
                    self.assertNotIn(b"hunter2", path.read_bytes(), path)
        self.assertGreater(scanned, 5)


if __name__ == "__main__":
    unittest.main()
