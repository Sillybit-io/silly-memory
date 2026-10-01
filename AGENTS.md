# AGENTS.md

Notes for coding agents working on silly-memory, a local memory engine shared by
Cursor, Claude Code, and OpenCode.

## End-to-end check with the real tools

`scripts/e2e_real_tools.py` checks the whole system with the real `claude`,
`cursor-agent`, and `opencode` CLIs. Run it after changing hooks, the OpenCode
plugin, event ingress, rendering, the MCP server, or the install and uninstall
scripts, and before a release:

```bash
python3 scripts/e2e_real_tools.py                 # all three tools, memory skills (MCP off)
python3 scripts/e2e_real_tools.py --mcp           # the same, installed with --mcp
python3 scripts/e2e_real_tools.py --tools claude-code,opencode
python3 scripts/e2e_real_tools.py --keep          # keep the throwaway folder
```

It builds a throwaway HOME and a dummy git project under the system temp folder,
installs silly-memory there from this checkout, and prints a report that answers:

1. Did the install script work (and does `memdoctor` agree)?
2. Does each tool's session reach memory (start, prompt, reply, turn end)?
3. Does it record a "remember that …" fact correctly (explicit, score 1.0)?
4. Does each tool answer from memory on a prompt?
5. Do the tools get each other's facts?
6. Can each tool use the memory tools? By default: no project file names the
   MCP server, and each tool stores a fact with the add-memory skill and finds
   one with the query-memory skill. With `--mcp`: each project gets its own MCP
   entry (nothing global), and each tool calls `memory_recall`. Run both modes
   after changing either path.
7. Did the uninstall remove every tool's artifacts and keep the data?

What to know before running it:

- It makes real model calls: a few short prompts per tool. Claude Code uses
  `haiku` and OpenCode a free `opencode/*` model by default; set
  `SILLY_E2E_CLAUDE_MODEL`, `SILLY_E2E_CURSOR_MODEL`, or
  `SILLY_E2E_OPENCODE_MODEL` to change them, and `SILLY_E2E_TIMEOUT` for the
  per-call limit (seconds, default 300).
- It never writes to your real HOME, settings, or memory store. The CLIs run with
  a scrubbed environment and HOME set to the throwaway folder. On macOS, that
  folder's `Library/Keychains` links to yours so Claude Code and cursor-agent can
  use their existing logins.
- Log in first: `claude` (then `/login`) and `cursor-agent login`. OpenCode needs
  no login. A tool whose CLI is missing or logged out is reported as SKIP with the
  fix; Cursor is then still checked through its installed hook and project rule.
- Exit status is 0 when nothing failed (skips allowed) and 1 when a check failed.
  After a failure the folder is kept; its `report.md` and `logs/` hold every
  command's output. Never commit those results.

## Other checks

- Unit suite (clear `SILLY_MEMORY_HOME` and `MEMORY_BIN` first, so no real home
  leaks in):
  `python3 -m unittest discover -s memory/tests -p "test_*.py"`. Perf tests record
  and skip on overshoot; the lock-contention and CoV checks can fail on a heavily
  loaded machine.
- Shell scripts: `shellcheck install.sh upgrade.sh uninstall.sh bootstrap.sh
  memory/install-zsh.sh memory/lib/memory_system/bash_helpers.sh
  cursor-extras/hooks/memory-hook.sh claude-code-extras/hooks/silly-memory-hook.sh`
- Types: `pyright -p pyrightconfig.json` (four known errors in `scope.py`).
- Read `memory/tests/README.md` before editing engine code, and
  `docs/architecture.md` for how the pieces fit.
