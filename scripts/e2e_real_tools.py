#!/usr/bin/env python3
"""End-to-end check of silly-memory with the real Cursor, Claude Code, and OpenCode CLIs.

Builds a throwaway HOME and a dummy git project under the system temp folder,
installs silly-memory into that HOME from this checkout, drives each tool's real
CLI against the project, and answers these questions:

  1. install    Does install.sh wire all three tools (and does memdoctor agree)?
  2. capture    Does each tool's session reach memory (its events, with its name)?
  3. record     Is each tool's "remember that ..." fact stored as an explicit fact?
  4. recall     Does each tool answer from memory when asked?
  5. share      Do the tools see each other's facts?
  6. tools      Can each tool use the memory tools? By default the MCP server is
                off: no project file may name it, and each tool must store a fact
                through the add-memory skill and find one through the
                query-memory skill, both by running the memory CLI. With --mcp:
                each project gets its own MCP entry (nothing global), and each
                tool calls the server's memory_recall.
  7. uninstall  Does uninstall.sh remove every tool's artifacts and keep the data?

Safety: nothing is written to your real HOME, settings, or memory store. Each CLI
runs with HOME set to the throwaway folder and a scrubbed environment. On macOS,
that folder's Library/Keychains links to yours, so Claude Code and cursor-agent can
use the logins you already have (they may refresh a token there, as in normal use).
Cursor's CLI settings file is copied in read-only. OpenCode runs a free opencode/*
model, so it needs no login. The script makes real model calls: a few short
prompts per tool.

A tool whose CLI is missing or not logged in is reported as SKIP, not FAIL, with
what to do. When cursor-agent cannot run, Cursor's side is still checked through
its installed hook with native Cursor payloads and through the project rule Cursor
loads; the report says so.

Usage:
    python3 scripts/e2e_real_tools.py [--tools claude-code,cursor,opencode] [--mcp] [--keep]

Environment: SILLY_E2E_CLAUDE_MODEL (default "haiku"), SILLY_E2E_CURSOR_MODEL
(default: cursor-agent's own default), SILLY_E2E_OPENCODE_MODEL (default
"opencode/big-pickle"), SILLY_E2E_TIMEOUT (seconds per CLI call, default 300).
CURSOR_API_KEY, CURSOR_AUTH_TOKEN, ANTHROPIC_API_KEY, and CLAUDE_CODE_OAUTH_TOKEN
pass through when set.

Exit status: 0 when no check failed (skips allowed), 1 when one did, 2 on bad usage.
The throwaway folder is deleted after a clean run and kept after a failure (or
with --keep); its logs/ folder holds every command's output.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REAL_HOME = Path.home()
TOOLS = ("claude-code", "cursor", "opencode")
PASS_THROUGH = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "SHELL", "TMPDIR",
    "CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
)
# What each tool is asked to remember, and how the fact is phrased.
SUBJECTS = {
    "claude-code": ("release codename", "release"),
    "cursor": ("staging database", "database"),
    "opencode": ("on-call channel", "channel"),
}
# Every tool's turn: start, prompt, reply, turn end (OpenCode's `run` has no session end).
CAPTURED = {"sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop"}


@dataclass
class Check:
    question: str
    tool: str
    status: str  # PASS, FAIL, SKIP
    detail: str


@dataclass
class Run:
    root: Path
    timeout: int
    tools: tuple[str, ...]
    mcp: bool = False
    checks: list[Check] = field(default_factory=list)
    tokens: dict[str, str] = field(default_factory=dict)
    live: dict[str, bool] = field(default_factory=dict)
    calls: int = 0

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def project(self) -> Path:
        return self.root / "project"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def memory_home(self) -> Path:
        return self.home / ".silly-memory"

    def add(self, question: str, tool: str, status: str, detail: str) -> None:
        self.checks.append(Check(question, tool, status, detail))
        mark = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "skip"}[status]
        print(f"  [{mark}] {question:<9} {tool:<11} {detail}", flush=True)

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: os.environ[k] for k in PASS_THROUGH if k in os.environ}
        env.update(HOME=str(self.home), MEMORY_SKIP_MODEL_DOWNLOAD="1")
        env.update(extra)
        return env

    def run(self, name: str, argv: list[str], *, stdin: str | None = None, cwd: Path | None = None,
            timeout: int | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        """Run one command in the throwaway HOME; its output goes to logs/<n>-<name>.log."""
        self.calls += 1
        log = self.logs / f"{self.calls:02d}-{name}.log"
        try:
            proc = subprocess.run(
                argv, input=stdin, cwd=str(cwd or self.project), env=self.env(**env),
                capture_output=True, text=True, timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            proc = subprocess.CompletedProcess(argv, 124, out, f"timed out after {timeout or self.timeout}s")
        log.write_text(f"$ {' '.join(argv)}\n--- rc={proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}\n",
                       encoding="utf-8")
        return proc

    # --- the project's store -------------------------------------------------

    def store(self) -> Path | None:
        marker = self.project / ".silly-memory" / "memory-id"
        return self.memory_home / marker.read_text(encoding="utf-8").strip() if marker.is_file() else None

    def events(self) -> list[dict]:
        store = self.store()
        log = store / "events.jsonl" if store else None
        if not log or not log.is_file():
            return []
        rows = []
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def explicit_fact(self, token: str) -> str | None:
        """Where ``token`` is stored as an explicit fact with score 1.0, or None."""
        store = self.store()
        for bank in sorted((store / "memory-bank").glob("*.md")) if store else []:
            lines = bank.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, start=1):
                if token not in line:
                    continue
                sidecar = bank.with_name(f"{bank.name}.score.json")
                scores = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
                entry = scores.get(f"{bank.name}:{number}", {})
                if "#explicit" in line and entry.get("score") == 1.0 and "explicit" in entry.get("tags", []):
                    return f"{bank.name}:{number}"
        return None

    def in_pack(self, token: str) -> list[str]:
        """Which of the places a tool loads memory from hold ``token``."""
        store = self.store()
        places = {
            "pack": store / "context-pack.md" if store else None,
            "cursor rule": self.project / ".cursor" / "rules" / "_memory-context.mdc",
            "claude rule": self.project / ".claude" / "rules" / "_memory-context.md",
        }
        return [name for name, path in places.items() if path and path.is_file() and token in path.read_text(encoding="utf-8")]

    def memory(self, *args: str, name: str) -> subprocess.CompletedProcess[str]:
        return self.run(name, [sys.executable, str(self.memory_home / "bin" / "memory"), *args], timeout=120)

    def memory_commands(self, events: list[dict], tool: str, subcommand: str) -> list[str]:
        """Shell commands ``tool`` ran that call the memory CLI's ``subcommand``."""
        commands = [e.get("payload", {}).get("command") or "" for e in events
                    if e.get("source") == tool and e.get("hook") == "afterShellExecution"]
        return [c for c in commands if "bin/memory" in c and re.search(rf"\b{subcommand}\b", c)]


# --- the tools ---------------------------------------------------------------


def cursor_binary() -> str | None:
    return shutil.which("cursor-agent") or shutil.which("agent")


def ask(run: Run, tool: str, prompt: str, name: str, *, allow: str = "") -> subprocess.CompletedProcess[str]:
    """Run one prompt; ``allow`` is "" (nothing extra), "mcp" (the memory_recall tool), or "skills" (skills and python3)."""
    if tool == "claude-code":
        argv = ["claude", "-p", prompt, "--output-format", "text",
                "--model", os.environ.get("SILLY_E2E_CLAUDE_MODEL", "haiku")]
        if allow == "mcp":
            argv += ["--allowedTools", "mcp__silly-memory__memory_recall"]
        elif allow == "skills":
            argv += ["--allowedTools", "Skill", "Bash(python3:*)"]
        return run.run(name, argv)
    if tool == "cursor":
        argv = [cursor_binary() or "cursor-agent", "--trust", "--print", "--output-format", "text",
                "--workspace", str(run.project)]
        if allow == "mcp":
            argv.append("--approve-mcps")
        elif allow == "skills":
            argv.append("--force")
        if os.environ.get("SILLY_E2E_CURSOR_MODEL"):
            argv += ["--model", os.environ["SILLY_E2E_CURSOR_MODEL"]]
        return run.run(name, argv, stdin=prompt + "\n")
    # On stdin: `opencode run "<prompt>"` records a one-argument message inside quotes.
    argv = ["opencode", "run", "--standalone", "--model", os.environ.get("SILLY_E2E_OPENCODE_MODEL", "opencode/big-pickle")]
    if allow:
        argv.append("--auto")
    return run.run(name, argv, stdin=prompt)


def answer_text(proc: subprocess.CompletedProcess[str]) -> str:
    """The model's reply without terminal color codes."""
    return re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)


def cursor_hook(run: Run, payload: dict, name: str) -> subprocess.CompletedProcess[str]:
    """What Cursor itself would send to the installed hook."""
    shim = run.home / ".cursor" / "hooks" / "memory-hook.sh"
    return run.run(name, ["bash", str(shim)], stdin=json.dumps(payload))


def failure_hint(tool: str, proc: subprocess.CompletedProcess[str]) -> str | None:
    """A known login or setup problem in a CLI's output, as advice; None when unknown."""
    text = (proc.stdout + proc.stderr).lower()
    if tool == "cursor" and ("authentication required" in text or "agent login" in text or "keychain is locked" in text):
        return "cursor-agent is not logged in: run `cursor-agent login` (or set CURSOR_API_KEY), then rerun"
    if tool == "claude-code" and ("not logged in" in text or "/login" in text or "invalid api key" in text):
        return "Claude Code is not logged in: run `claude` once and /login (or set ANTHROPIC_API_KEY), then rerun"
    if tool == "opencode" and "model unavailable" in text:
        return "the OpenCode model is unavailable: set SILLY_E2E_OPENCODE_MODEL to one `opencode models` lists"
    return None


# --- the steps ---------------------------------------------------------------


def prepare(run: Run) -> None:
    for path in (run.home, run.project, run.logs):
        path.mkdir(parents=True)
    keychains = REAL_HOME / "Library" / "Keychains"
    if sys.platform == "darwin" and keychains.is_dir():
        (run.home / "Library").mkdir()
        (run.home / "Library" / "Keychains").symlink_to(keychains)
    cursor_cli_config = REAL_HOME / ".cursor" / "cli-config.json"
    (run.home / ".cursor").mkdir()
    if cursor_cli_config.is_file():
        shutil.copy2(cursor_cli_config, run.home / ".cursor" / "cli-config.json")
    (run.home / ".zshrc").write_text("# throwaway zshrc for the silly-memory end-to-end check\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(run.project)], check=True)
    (run.project / "README.md").write_text("# e2e demo project\n\nA throwaway project for silly-memory.\n", encoding="utf-8")


def check_install(run: Run) -> bool:
    argv = ["bash", str(REPO / "install.sh"), "--tools", ",".join(TOOLS)] + (["--mcp"] if run.mcp else [])
    proc = run.run("install", argv, cwd=REPO, timeout=900)
    if proc.returncode != 0:
        run.add("install", "all", "FAIL", f"install.sh exited {proc.returncode} (see logs)")
        return False
    doctor = run.memory("doctor", "--json", name="doctor")
    try:
        checks = {c["name"]: c for c in json.loads(doctor.stdout).get("checks", [])}
    except (ValueError, AttributeError):
        checks = {}
    for name in ("tools", "mcp"):
        check = checks.get(name)
        if not check:
            run.add("install", "all", "FAIL", f"memdoctor has no '{name}' check (see logs)")
        elif check.get("level") == "error":
            run.add("install", "all", "FAIL", f"memdoctor {name}: {check.get('detail', check)}")
        else:
            run.add("install", "all", "PASS", f"memdoctor {name}: {check.get('detail', check.get('level'))}")
    expected = {
        "claude-code": [run.home / ".claude" / "hooks" / "silly-memory-hook.sh", run.home / ".claude" / "rules" / "memory-recall.md"],
        "cursor": [run.home / ".cursor" / "hooks" / "memory-hook.sh", run.home / ".cursor" / "hooks.json"],
        "opencode": [run.home / ".config" / "opencode" / "plugins" / "silly-memory.js"],
    }
    for tool, paths in expected.items():
        missing = [str(p.relative_to(run.home)) for p in paths if not p.is_file()]
        run.add("install", tool, "FAIL" if missing else "PASS", f"missing {missing}" if missing else "hooks or plugin, and rule in place")
    global_files = [p for p in (run.home / ".claude.json", run.home / ".cursor" / "mcp.json")
                    if p.is_file() and "memory-mcp" in p.read_text(encoding="utf-8")]
    run.add("install", "all", "FAIL" if global_files else "PASS",
            f"registered globally in {global_files}" if global_files else "no MCP server registered globally")
    return True


def project_server(run: Run, tool: str) -> dict | None:
    """The silly-memory server in the project's own config for ``tool``, if ours."""
    rel, key = {"claude-code": (".mcp.json", "mcpServers"), "cursor": (".cursor/mcp.json", "mcpServers"),
                "opencode": ("opencode.json", "mcp")}[tool]
    path = run.project / rel
    try:
        entry = json.loads(path.read_text(encoding="utf-8")).get(key, {}).get("silly-memory")
    except (OSError, ValueError, AttributeError):
        return None
    return entry if isinstance(entry, dict) and "memory-mcp" in json.dumps(entry) else None


def check_project_mcp(run: Run) -> None:
    """After the first sessions, each project config names the server, with no machine path."""
    for tool in TOOLS:
        entry = project_server(run, tool)
        if entry is None:
            run.add("tools", tool, "FAIL", "the project's own config has no silly-memory server after its first session")
        elif str(run.home) in json.dumps(entry):
            run.add("tools", tool, "FAIL", "the project entry names this machine's home, so it cannot be committed")
        else:
            run.add("tools", tool, "PASS", "the project's own config has the silly-memory server (portable, committable)")
    approvals = run.project / ".claude" / "settings.local.json"
    approved = approvals.is_file() and "silly-memory" in json.loads(approvals.read_text(encoding="utf-8")).get("enabledMcpjsonServers", [])
    run.add("tools", "claude-code", "PASS" if approved else "FAIL",
            "pre-approved in .claude/settings.local.json" if approved else "not approved in .claude/settings.local.json")


def check_no_project_mcp(run: Run) -> None:
    """With MCP off (the default), no session wrote the server into a project file."""
    named = [tool for tool in TOOLS if project_server(run, tool) is not None]
    approvals = run.project / ".claude" / "settings.local.json"
    if approvals.is_file() and "silly-memory" in approvals.read_text(encoding="utf-8"):
        named.append("claude-code approval")
    run.add("tools", "all", "FAIL" if named else "PASS",
            f"MCP is off but the project config names the server for {named}" if named
            else "MCP off: no project file names the server, so there is nothing to approve")


def probe_cursor(run: Run) -> None:
    binary = cursor_binary()
    if not binary:
        run.live["cursor"] = False
        run.add("capture", "cursor", "SKIP", "cursor-agent is not installed; Cursor is checked through its hook")
        return
    proc = run.run("cursor-models", [binary, "--list-models"], timeout=60)
    hint = failure_hint("cursor", proc)
    run.live["cursor"] = proc.returncode == 0 and hint is None
    if not run.live["cursor"]:
        run.add("capture", "cursor", "SKIP", (hint or f"cursor-agent exited {proc.returncode}") + "; Cursor is checked through its hook")


def write_facts(run: Run, tool: str) -> None:
    """One session per tool that asks it to remember a fact."""
    subject, _ = SUBJECTS[tool]
    token = run.tokens[tool]
    # The fact is the first line of a "remember that" prompt; the instruction goes below it.
    prompt = f"remember that the {subject} for this project is {token}\nReply with just: noted"
    before = len(run.events())
    if run.live.get(tool):
        proc = ask(run, tool, prompt, f"{tool}-remember")
        hint = failure_hint(tool, proc)
        if proc.returncode != 0 or hint:
            run.live[tool] = False
            run.add("capture", tool, "SKIP" if hint else "FAIL", hint or f"the {tool} CLI exited {proc.returncode} (see logs)")
            return
        time.sleep(2)
    else:
        # Cursor without its CLI: the same session, as Cursor sends it to the hook.
        root = str(run.project)
        conversation = f"e2e-{secrets.token_hex(4)}"
        for payload in (
            {"hook_event_name": "sessionStart", "conversation_id": conversation, "workspace_roots": [root]},
            {"hook_event_name": "beforeSubmitPrompt", "conversation_id": conversation, "workspace_roots": [root], "prompt": prompt},
            {"hook_event_name": "afterAgentResponse", "conversation_id": conversation, "workspace_roots": [root], "text": "noted"},
            {"hook_event_name": "stop", "conversation_id": conversation, "workspace_roots": [root], "status": "completed"},
            {"hook_event_name": "sessionEnd", "conversation_id": conversation, "workspace_roots": [root], "reason": "completed"},
        ):
            cursor_hook(run, payload, f"cursor-hook-{payload['hook_event_name']}")

    new = run.events()[before:]
    seen = sorted({e.get("hook") for e in new if e.get("source") == tool})
    via = "" if run.live.get(tool) else " (through its hook)"
    missing = sorted(CAPTURED - set(seen))
    if not missing:
        run.add("capture", tool, "PASS", f"events recorded{via}: {', '.join(seen)}")
    else:
        run.add("capture", tool, "FAIL", f"missing {', '.join(missing)} from {tool}{via}; got {seen or 'none'}")

    where = run.explicit_fact(token)
    if where:
        run.add("record", tool, "PASS", f"explicit fact, score 1.0, at {where}; in {', '.join(run.in_pack(token)) or 'no rule yet'}")
        return
    run.add("record", tool, "FAIL", "the fact was not stored when the session ended; processing it by hand so later checks can run")
    run.memory("process", "--workspace", str(run.project), name=f"{tool}-manual-process")
    where = run.explicit_fact(token)
    if not where:
        run.add("record", tool, "FAIL", "still not stored after `memory process`")


def read_facts(run: Run, tool: str) -> None:
    """Ask a tool for every fact; its own answers recall, the others' answer sharing."""
    labels = {t: SUBJECTS[t][1] for t in TOOLS}
    if not run.live.get(tool) and tool != "cursor":
        run.add("recall", tool, "SKIP", f"the {tool} CLI could not run; see the capture check above")
        return
    if run.live.get(tool):
        prompt = ("Using only what your project memory says (do not run tools or commands), what are this project's "
                  "release codename, staging database, and on-call channel? Reply in one line as "
                  "release=<value>; database=<value>; channel=<value>. Use unknown for anything you do not know.")
        proc = ask(run, tool, prompt, f"{tool}-recall")
        hint = failure_hint(tool, proc)
        if proc.returncode != 0 or hint:
            run.add("recall", tool, "SKIP" if hint else "FAIL", hint or f"the {tool} CLI exited {proc.returncode} (see logs)")
            return
        reply = answer_text(proc)
        source = "its reply"
    else:
        rule = run.project / ".cursor" / "rules" / "_memory-context.mdc"
        reply = rule.read_text(encoding="utf-8") if rule.is_file() else ""
        source = "the Cursor project rule (cursor-agent could not run)"
    for writer in TOOLS:
        token = run.tokens[writer]
        question = "recall" if writer == tool else "share"
        who = f"its own {labels[writer]}" if writer == tool else f"{writer}'s {labels[writer]}"
        if token in reply:
            run.add(question, tool, "PASS", f"{who} ({token}) is in {source}")
        else:
            run.add(question, tool, "FAIL", f"{who} ({token}) is missing from {source}: {reply.strip()[:160]!r}")


def check_mcp(run: Run, tool: str) -> None:
    """The tool can call memory_recall on the silly-memory server."""
    token = run.tokens["cursor"]  # stored for every run, through cursor-agent or Cursor's hook
    if not run.live.get(tool):
        run.add("tools", tool, "SKIP", f"the {tool} CLI could not run to call memory_recall; see the project config check above")
        return
    prompt = (f"Call the memory_recall tool of the silly-memory MCP server with query {token} and scope workspace. "
              "Reply with the exact text the tool returned, nothing else.")
    proc = ask(run, tool, prompt, f"{tool}-mcp", allow="mcp")
    reply = answer_text(proc)
    ok = proc.returncode == 0 and token in reply
    run.add("tools", tool, "PASS" if ok else "FAIL",
            f"memory_recall returned {token}" if ok else f"no {token} in the reply: {reply.strip()[:160]!r}")


def check_skills(run: Run, tool: str) -> None:
    """Without MCP, the tool stores a fact with the add-memory skill and finds one with query-memory."""
    if not run.live.get(tool):
        run.add("tools", tool, "SKIP", f"the {tool} CLI could not run to use the memory skills")
        return
    token = f"window-{secrets.token_hex(3)}"
    before = len(run.events())
    prompt = (f"Use the add-memory skill to save this fact to project memory: the deploy window for this project "
              f"is {token}. Reply with just: saved")
    proc = ask(run, tool, prompt, f"{tool}-skill-add", allow="skills")
    ran = run.memory_commands(run.events()[before:], tool, "add")
    where = run.explicit_fact(token)
    if proc.returncode == 0 and where:
        run.add("tools", tool, "PASS", f"add-memory stored {token} as an explicit fact, score 1.0, at {where}"
                + (f" (ran `memory add` {len(ran)}x)" if ran else ""))
    else:
        run.add("tools", tool, "FAIL", f"add-memory did not store {token}; commands run: {ran or 'none'}; "
                f"reply: {answer_text(proc).strip()[:160]!r}")

    wanted = run.tokens["cursor"]  # stored for every run, through cursor-agent or Cursor's hook
    before = len(run.events())
    prompt = (f"Use the query-memory skill to search project memory for {wanted}. "
              "Reply with the exact line it found, nothing else.")
    proc = ask(run, tool, prompt, f"{tool}-skill-recall", allow="skills")
    ran = run.memory_commands(run.events()[before:], tool, "recall")
    reply = answer_text(proc)
    if proc.returncode == 0 and ran and wanted in reply:
        run.add("tools", tool, "PASS", f"query-memory ran `memory recall` and returned {wanted}")
    else:
        why = "it ran no `memory recall`" if not ran else f"no {wanted} in the reply"
        run.add("tools", tool, "FAIL", f"query-memory: {why}: {reply.strip()[:160]!r}")


def check_uninstall(run: Run) -> None:
    store = run.store()
    banks_before = {p.name: p.read_bytes() for p in (store / "memory-bank").glob("*.md")} if store else {}
    proc = run.run("uninstall", ["bash", str(REPO / "uninstall.sh"), "--confirm", "--remove-zsh-helper"], cwd=REPO, timeout=300)
    if proc.returncode != 0:
        run.add("uninstall", "all", "FAIL", f"uninstall.sh exited {proc.returncode} (see logs)")
        return
    leftovers = {
        "claude-code": [run.home / ".claude" / "hooks" / "silly-memory-hook.sh", run.home / ".claude" / "rules" / "memory-recall.md"],
        "cursor": [run.home / ".cursor" / "hooks" / "memory-hook.sh", run.home / ".cursor" / "rules" / "memory-recall.mdc"],
        "opencode": [run.home / ".config" / "opencode" / "plugins" / "silly-memory.js"],
    }
    settings = {
        "claude-code": [(run.home / ".claude" / "settings.json", "silly-memory-hook.sh")],
        "cursor": [(run.home / ".cursor" / "hooks.json", "memory-hook.sh")],
        "opencode": [],
    }
    for tool in TOOLS:
        left = [str(p.relative_to(run.home)) for p in leftovers[tool] if p.exists()]
        left += [str(p.relative_to(run.home)) for p, needle in settings[tool] if p.is_file() and needle in p.read_text(encoding="utf-8")]
        if project_server(run, tool) is not None:
            left.append(f"the project's {tool} MCP entry")
        run.add("uninstall", tool, "FAIL" if left else "PASS",
                f"left behind: {left}" if left else "its hooks, rule, skills, and project MCP entry are gone")
    kept = store is not None and all((store / "memory-bank" / n).read_bytes() == b for n, b in banks_before.items())
    engine_gone = not (run.memory_home / "bin").exists()
    run.add("uninstall", "all", "PASS" if kept and engine_gone else "FAIL",
            "engine removed; every memory-bank file kept byte for byte" if kept and engine_gone
            else f"engine removed: {engine_gone}; memory kept: {kept}")


# --- report ------------------------------------------------------------------

QUESTIONS = {
    "install": "Did the install script work?",
    "capture": "Does each tool's session reach memory?",
    "record": "Does it record memory correctly?",
    "recall": "Does it return what it should on a prompt?",
    "share": "Do the tools get memory from each other?",
    "tools": "Can each tool use the memory tools?",
    "uninstall": "Did the uninstall work?",
}


def report(run: Run) -> str:
    lines = ["# silly-memory end-to-end report", ""]
    for key, question in QUESTIONS.items():
        rows = [c for c in run.checks if c.question == key]
        statuses = {c.status for c in rows}
        verdict = "no" if "FAIL" in statuses else "yes" if "PASS" in statuses else "not checked"
        lines.append(f"## {question} {verdict}")
        lines += [f"- {c.status} {c.tool}: {c.detail}" for c in rows] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check silly-memory end to end with the real tool CLIs.")
    parser.add_argument("--tools", default=",".join(TOOLS), help="tools whose CLIs to drive (default: all three)")
    parser.add_argument("--mcp", action="store_true",
                        help="install with --mcp and check the MCP server instead of the skills")
    parser.add_argument("--keep", action="store_true", help="keep the throwaway folder even after a clean run")
    args = parser.parse_args(argv)
    tools = tuple(t for t in args.tools.split(",") if t)
    unknown = [t for t in tools if t not in TOOLS]
    if unknown:
        print(f"unknown tool(s): {', '.join(unknown)} (expected: {', '.join(TOOLS)})", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="silly-memory-e2e-"))
    run = Run(root=root, timeout=int(os.environ.get("SILLY_E2E_TIMEOUT", "300")), tools=tools, mcp=args.mcp)
    run.tokens = {tool: f"{SUBJECTS[tool][1]}-{secrets.token_hex(3)}" for tool in TOOLS}
    print(f"silly-memory end-to-end check in {root} (memory tools: {'MCP' if run.mcp else 'skills, MCP off'})", flush=True)
    prepare(run)
    if check_install(run):
        for tool, binary in (("claude-code", "claude"), ("opencode", "opencode")):
            run.live[tool] = tool in tools and shutil.which(binary) is not None
            if tool in tools and not run.live[tool]:
                run.add("capture", tool, "SKIP", f"`{binary}` is not on PATH")
        if "cursor" in tools:
            probe_cursor(run)
        else:
            run.live["cursor"] = False
        for tool in ("claude-code", "opencode", "cursor"):
            if tool in tools or tool == "cursor":
                write_facts(run, tool)
        if run.mcp:
            check_project_mcp(run)
        for tool in ("claude-code", "opencode", "cursor"):
            if tool in tools:
                read_facts(run, tool)
        for tool in ("claude-code", "opencode", "cursor"):
            if tool in tools:
                (check_mcp if run.mcp else check_skills)(run, tool)
        if not run.mcp:
            check_no_project_mcp(run)
        check_uninstall(run)

    text = report(run)
    (root / "report.md").write_text(text, encoding="utf-8")
    print("\n" + text)
    failed = any(c.status == "FAIL" for c in run.checks)
    if failed or args.keep:
        print(f"Kept {root} (report.md and logs/).")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
