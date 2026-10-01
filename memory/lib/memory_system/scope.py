from __future__ import annotations

from pathlib import Path

from .system.config import load_config
from .paths import bank_path, cli_command, ensure_layout, global_store, workspace_store
from .recall.sessions import render_recent_sessions


GLOBAL_FILES = {"learned-memories.md", "audienceContext.md", "conventions.md"}


def read_bank_slice(store: Path, filenames: list[str], max_chars: int) -> str:
    parts: list[str] = []
    used = 0
    for name in filenames:
        path = bank_path(store, name)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        chunk = f"### {name}\n\n{text}\n"
        if used + len(chunk) > max_chars:
            remaining = max_chars - used
            if remaining > 200:
                parts.append(chunk[:remaining] + "\n…\n")
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)


def merge_context_sources(workspace_root: Path, token_cap: int | None = None) -> str:
    cfg = load_config()
    cap = token_cap or int(cfg.get("context_pack_token_cap", 16000))
    max_chars = cap * 4

    ws = workspace_store(workspace_root)
    gs = global_store()
    ensure_layout(ws)
    ensure_layout(gs)

    bank_files = cfg.get("bank_files", [])
    global_files = [f for f in bank_files if f in GLOBAL_FILES or f == "README.md"]
    workspace_files = [f for f in bank_files if f not in GLOBAL_FILES and f != "README.md"]

    # Reserve space for the non-bank tail (work-state, recent observations) and
    # structural overhead so the WHOLE rendered pack stays within max_chars. The
    # pack is injected every turn via the alwaysApply rule, so the cap is a hard
    # budget, not just a per-slice limit.
    work_state_reserve = 2000
    sessions_reserve = 1200
    observations_reserve = 3000
    overhead_reserve = 800
    bank_budget = max(
        1000, max_chars - work_state_reserve - sessions_reserve - observations_reserve - overhead_reserve
    )

    sections: list[str] = ["# Memory Context Pack", ""]
    global_budget = bank_budget // 3
    workspace_budget = bank_budget - global_budget

    gtext = read_bank_slice(gs, global_files, global_budget)
    if gtext:
        sections.append("## Global memory")
        sections.append(gtext)

    wtext = read_bank_slice(ws, workspace_files, workspace_budget)
    if wtext:
        sections.append("## Workspace memory")
        sections.append(wtext)

    work_state = ws / "work-state.md"
    if work_state.exists():
        wsnap = work_state.read_text(encoding="utf-8").strip()
        if wsnap:
            sections.append("## Current work state")
            sections.append(wsnap[:work_state_reserve])

    timeline = render_recent_sessions(ws)
    if timeline:
        sections.append("## Recent sessions")
        sections.append(timeline[:sessions_reserve])

    obs = ws / "observations.md"
    if obs.exists():
        otext = obs.read_text(encoding="utf-8").strip()
        if otext and len(otext) > 50:
            sections.append("## Recent observations")
            sections.append(otext[-observations_reserve:])

    footer = (
        f"Deep recall: run `{cli_command()} recall \"<query>\"` "
        "from the workspace root when facts are missing."
    )
    body = "\n".join(sections)
    # Hard safety net: guarantee the total never exceeds the cap, always keeping
    # the recall footer reachable. RULE_HEADER_RESERVE accounts for the frontmatter
    # that render_rule_file prepends (~270 chars) so the *injected file*, not just
    # this pack body, stays within max_chars.
    rule_header_reserve = 320
    budget_for_body = max(0, max_chars - len(footer) - rule_header_reserve - 4)
    if len(body) > budget_for_body:
        body = body[:budget_for_body].rstrip() + "\n…"
    return body + "\n\n" + footer + "\n"
