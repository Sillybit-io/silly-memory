"""Status dashboard core — workspaces, tasks, paths views for ``memstatus``.

Read this first if you are new to the codebase:
  - This is the biggest piece of the status subpackage. It collects
    per-workspace bank summaries, staging counts, action items, last
    event timestamps, and the learning-section line totals, then renders
    them as either text or JSON.
  - ``VIEWS`` enumerates the supported view names (workspaces, paths,
    tasks, default dashboard); ``show_view`` dispatches to the matching
    renderer. Adding a new view means extending both.
  - Defensive reads everywhere: every file read tolerates a missing or
    malformed file by returning ``"—"`` or an empty list, so a single
    corrupted bank cannot blank out the entire dashboard.

Public interface (imported elsewhere): ``VIEWS``, ``show_view``,
    ``iter_workspaces``, ``render_workspaces``, ``render_paths``,
    ``collect_tasks``, ``render_tasks_json``, ``render_tasks``,
    ``_parse_action_items``, plus the helpers banner reaches into
    (``_bank_summary``, ``_count_lines``, ``_count_bullets``,
    ``_last_event``, ``_learning_section_lines``, ``_mtime``,
    ``_staging_breakdown``).
Depends on: system.config, paths,
    learning.contradiction (lazy where used).
Used by: status (package init re-export), status.banner.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from memory_system.system.config import memory_home
from memory_system.paths import GLOBAL_ID, bank_path, generated_rule_path, global_store, workspace_store


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())


def _count_bullets(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip().startswith("- "))


def _mtime(path: Path) -> str:
    if not path.exists():
        return "—"
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _last_event(store: Path) -> str:
    ev = store / "events.jsonl"
    if not ev.exists():
        return "—"
    lines = [ln for ln in ev.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return "—"
    try:
        rec = json.loads(lines[-1])
        return f"{rec.get('hook', '?')} @ {str(rec.get('ts', ''))[:19]}"
    except json.JSONDecodeError:
        return "—"


def _bank_summary(store: Path) -> list[tuple[str, int]]:
    bank = store / "memory-bank"
    out: list[tuple[str, int]] = []
    if bank.exists():
        for md in sorted(bank.glob("*.md")):
            out.append((md.name, _count_bullets(md)))
    return out


def _staging_breakdown(store: Path) -> dict[str, int]:
    staging = store / "staging" / "pending.jsonl"
    counts: dict[str, int] = {}
    if not staging.exists():
        return counts
    for ln in staging.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            counts[json.loads(ln).get("category", "?")] = counts.get(json.loads(ln).get("category", "?"), 0) + 1
        except json.JSONDecodeError:
            continue
    return counts


def _score_buckets(sidecar: Path) -> tuple[int, int, int]:
    """Return (low<0.3, 0.3<=mid<=0.7, high>0.7) counts from a score sidecar.

    Sidecars produced by lifecycle.scoring (T11) are tolerated when missing,
    empty, malformed, or non-dict. The function never raises — a broken
    sidecar simply yields (0, 0, 0).
    """
    if not sidecar.exists():
        return (0, 0, 0)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (0, 0, 0)
    if not isinstance(data, dict):
        return (0, 0, 0)
    low = mid = high = 0
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        try:
            score = float(entry.get("score", 0.5))
        except (TypeError, ValueError):
            continue
        if score < 0.3:
            low += 1
        elif score <= 0.7:
            mid += 1
        else:
            high += 1
    return (low, mid, high)


def _reinforcement_totals(bank_dir: Path) -> tuple[int, int]:
    """Sum (corrections_count, reinforcements_count) across every sidecar."""
    if not bank_dir.exists():
        return (0, 0)
    corr_total = 0
    reinf_total = 0
    for sidecar in sorted(bank_dir.glob("*.score.json")):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            try:
                corr_total += int(entry.get("corrections_count", 0) or 0)
                reinf_total += int(entry.get("reinforcements_count", 0) or 0)
            except (TypeError, ValueError):
                continue
    return (corr_total, reinf_total)


def _contradictions_count(bank_dir: Path) -> int:
    """Invoke T17 detector live against current bank files.

    Import is intentionally local so a circular-import or missing-module
    failure cannot break the dashboard for users on a partially-migrated
    install. Returns 0 when nothing can be computed.
    """
    if not bank_dir.exists():
        return 0
    bank_files = sorted(bank_dir.glob("*.md"))
    if not bank_files:
        return 0
    try:
        from memory_system.learning.contradiction import detect_contradictions

        return len(detect_contradictions(bank_files))
    except Exception:
        return 0


def _last_ai_text_ts(store: Path) -> str:
    """Return the ts of the most recent ai-text-log.jsonl record, or placeholder."""
    log = store / "ai-text-log.jsonl"
    if not log.exists():
        return "not yet recorded"
    try:
        raw = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return "not yet recorded"
    if not raw:
        return "not yet recorded"
    try:
        rec = json.loads(raw[-1])
    except json.JSONDecodeError:
        return "not yet recorded"
    if not isinstance(rec, dict):
        return "not yet recorded"
    ts = str(rec.get("ts") or "").strip()
    return ts or "not yet recorded"


def _learning_section_lines(store: Path) -> list[str]:
    """Render the appended 'Learning Status' block (T29).

    Every input is best-effort: missing sidecars / state files / logs render
    as zeros or 'not yet recorded' instead of crashing the dashboard.
    """
    bank_dir = store / "memory-bank"
    lines: list[str] = []
    lines.append("")
    lines.append("Learning Status")
    lines.append("  score distribution (low<0.3 · mid 0.3-0.7 · high>0.7)")
    bank_files = sorted(bank_dir.glob("*.md")) if bank_dir.exists() else []
    if not bank_files:
        lines.append("    (no bank files yet)")
    else:
        for md in bank_files:
            sidecar = md.parent / f"{md.name}.score.json"
            low, mid, high = _score_buckets(sidecar)
            lines.append(f"    {md.name:<22}: low:{low} mid:{mid} high:{high}")
    corr_total, reinf_total = _reinforcement_totals(bank_dir)
    lines.append(f"  corrections applied: {corr_total}")
    lines.append(f"  reinforcements applied: {reinf_total}")
    lines.append(f"  recent contradictions: {_contradictions_count(bank_dir)}")
    lines.append(f"  AI-text log (last entry): {_last_ai_text_ts(store)}")
    return lines


VIEWS = ("pack", "observations", "staging", "bank", "events")


def show_view(workspace_root: Path, view: str, limit: int = 40) -> str:
    ws = workspace_store(workspace_root)
    if view == "pack":
        rule = generated_rule_path(workspace_root)
        return rule.read_text(encoding="utf-8") if rule.exists() else "(no injected pack yet)"
    if view == "observations":
        p = ws / "observations.md"
        if not p.exists():
            return "(no observations yet)"
        obs = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip().startswith("- ")]
        return "\n".join(obs[-limit:]) or "(no observations yet)"
    if view == "staging":
        p = ws / "staging" / "pending.jsonl"
        if not p.exists() or not p.read_text(encoding="utf-8").strip():
            return "(staging empty — nothing awaiting promotion)"
        out = []
        for ln in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            if not ln.strip():
                continue
            try:
                item = json.loads(ln)
                out.append(f"  [{item.get('confidence')}] #{item.get('category')}: {item.get('body', '')[:160]}")
            except json.JSONDecodeError:
                continue
        return "\n".join(out)
    if view == "bank":
        parts = []
        for label, store in (("workspace", ws), ("global", global_store())):
            bank = store / "memory-bank"
            if not bank.exists():
                continue
            for md in sorted(bank.glob("*.md")):
                text = md.read_text(encoding="utf-8").strip()
                if text:
                    parts.append(f"===== [{label}] {md.name} =====\n{text}")
        return "\n\n".join(parts) or "(bank empty)"
    if view == "events":
        p = ws / "events.jsonl"
        if not p.exists():
            return "(no events yet)"
        out = []
        for ln in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rec = json.loads(ln)
                out.append(f"  {str(rec.get('ts',''))[:19]}  {rec.get('hook','?')}")
            except json.JSONDecodeError:
                continue
        return "\n".join(out)
    return f"Unknown view '{view}'. Choose one of: {', '.join(VIEWS)}"


# --- Workspace registry -------------------------------------------------------

def iter_workspaces() -> list[dict]:
    """All known workspace stores (each created store writes a .meta.json).

    Returns dicts with id, root, open/done task counts, and last-activity time,
    sorted by most-recent activity first.
    """
    home = memory_home()
    out: list[dict] = []
    if not home.exists():
        return out
    for d in home.iterdir():
        if not d.is_dir() or d.name == GLOBAL_ID:
            continue
        meta_p = d / ".meta.json"
        if not meta_p.exists():
            continue  # not a workspace store (bin/lib/hooks/etc.)
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        tasks = _parse_action_items(bank_path(d, "actionItems.md"))
        ev = d / "events.jsonl"
        activity = ev if ev.exists() and ev.stat().st_size else (d / "observations.md")
        out.append(
            {
                "id": d.name,
                "root": meta.get("workspace_root", "(unknown)"),
                "open": sum(1 for t in tasks if not t["done"]),
                "done": sum(1 for t in tasks if t["done"]),
                "activity": activity.stat().st_mtime if activity.exists() else 0.0,
                "activity_str": _mtime(activity),
            }
        )
    out.sort(key=lambda w: w["activity"], reverse=True)
    return out


def render_workspaces() -> str:
    rows = iter_workspaces()
    lines = ["silly-memory — workspaces", "=" * 60]
    if not rows:
        lines.append("(no workspaces tracked yet — open a project and start a chat)")
        return "\n".join(lines) + "\n"
    lines.append(f"  {'#':>2}  {'open':>4} {'done':>4}  {'last activity':<16}  workspace")
    for i, w in enumerate(rows, 1):
        lines.append(
            f"  {i:>2}  {w['open']:>4} {w['done']:>4}  {w['activity_str']:<16}  {w['root']}"
        )
    lines.append("")
    lines.append("Inspect one:")
    lines.append("  memory tasks  --workspace <path>     # action items as a task list")
    lines.append("  memory status --workspace <path>     # full dashboard")
    return "\n".join(lines) + "\n"


def render_paths(workspace_root: Path, scope: str = "all") -> str:
    """List every markdown file in the memory store(s) with absolute paths.

    scope: 'workspace' | 'global' | 'all'. Output is copy/paste friendly so you
    can open or cat any memory file directly.
    """
    ws = workspace_store(workspace_root)
    gs = global_store()
    targets: list[tuple[str, Path]] = []
    if scope in ("workspace", "all"):
        targets.append(("workspace", ws))
    if scope in ("global", "all"):
        targets.append(("global", gs))

    lines = ["silly-memory — markdown files", "=" * 60]
    for label, store in targets:
        bank = store / "memory-bank"
        bank_files = sorted(bank.glob("*.md")) if bank.exists() else []
        derived = [p for p in (store / "observations.md", store / "work-state.md", store / "context-pack.md") if p.exists()]
        lines.append("")
        lines.append(f"[{label}] {store}")
        if bank_files:
            lines.append("  durable bank:")
            for p in bank_files:
                lines.append(f"    {_count_bullets(p):>3} facts  {p}")
        if derived:
            lines.append("  derived:")
            for p in derived:
                lines.append(f"    {_count_bullets(p):>3} items  {p}")
        if not bank_files and not derived:
            lines.append("  (no markdown files yet)")

    # The injected rule file lives in the workspace, not the store.
    if scope in ("workspace", "all"):
        rule = generated_rule_path(workspace_root)
        lines.append("")
        lines.append("[injected] rendered into every chat")
        lines.append(f"    {'ok' if rule.exists() else 'missing':>7}  {rule}")
    return "\n".join(lines) + "\n"


# --- Action items -------------------------------------------------------------

_ITEM_RE = re.compile(r"^- \[( |x)\]\s*(.*)$")
_DATE_RE = re.compile(r"^\[([^\]]+)\]\s*")
_TAGS_RE = re.compile(r"^((?:#[\w-]+\s*)+):\s*(.+)$")


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"{name}:\s*([^;]+?)(?:;|$)", text)
    if not m:
        return None
    val = m.group(1).strip().rstrip(".")
    return val if val and val.lower() != "none" else None


def _parse_action_items(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _ITEM_RE.match(raw.strip())
        if not m:
            continue
        done = m.group(1) == "x"
        rest = m.group(2).strip()
        dm = _DATE_RE.match(rest)
        date = dm.group(1) if dm else ""
        if dm:
            rest = rest[dm.end():]
        tags: list[str] = []
        title = rest
        tm = _TAGS_RE.match(rest)
        if tm:
            tags = re.findall(r"#([\w-]+)", tm.group(1))
            title = tm.group(2).strip()
        # Title is the text before the metadata separator " — ".
        body = title
        if " — " in title:
            title = title.split(" — ", 1)[0].strip()
        items.append(
            {
                "done": done,
                "date": date,
                "tags": [t for t in tags if t != "action"],
                "title": title,
                "owner": _field(body, "owner"),
                "due": _field(body, "due"),
                "jira": _field(body, "jira"),
            }
        )
    return items


def _render_task_block(items: list[dict], start: int = 1) -> list[str]:
    lines: list[str] = []
    for i, t in enumerate(items, start):
        box = "x" if t["done"] else " "
        lines.append(f"  {i:>3}. [{box}] {t['title']}")
        meta: list[str] = []
        if t["owner"]:
            meta.append(f"owner: {t['owner']}")
        if t["due"]:
            meta.append(f"due: {t['due']}")
        if t["jira"]:
            meta.append(f"jira: {t['jira']}")
        if t["date"] and t["date"] != t["due"]:
            meta.append(f"logged: {t['date']}")
        if t["tags"]:
            meta.append(" ".join(f"#{x}" for x in t["tags"]))
        if meta:
            lines.append(f"        {' · '.join(meta)}")
    return lines


def _filter_items(
    items: list[dict], status: str, tag: str | None, owner: str | None
) -> list[dict]:
    out = items
    if status == "open":
        out = [t for t in out if not t["done"]]
    elif status == "done":
        out = [t for t in out if t["done"]]
    if tag:
        out = [t for t in out if tag.lstrip("#").lower() in [x.lower() for x in t["tags"]]]
    if owner:
        out = [t for t in out if t["owner"] and owner.lower() in t["owner"].lower()]
    return out


def collect_tasks(
    workspace_root: Path,
    status: str = "open",
    tag: str | None = None,
    owner: str | None = None,
    all_workspaces: bool = False,
) -> dict:
    """Structured action-item data (used by JSON output and the text renderer)."""
    filters = {"status": status, "tag": tag.lstrip("#") if tag else None, "owner": owner}
    if all_workspaces:
        groups: list[dict] = []
        total = 0
        for w in iter_workspaces():
            store = memory_home() / w["id"]
            items = _filter_items(
                _parse_action_items(bank_path(store, "actionItems.md")), status, tag, owner
            )
            if not items:
                continue
            total += len(items)
            groups.append(
                {"workspace": w["root"], "id": w["id"], "count": len(items), "items": items}
            )
        return {"scope": "all", "filters": filters, "total": total, "workspaces": groups}

    store = workspace_store(workspace_root)
    items = _filter_items(
        _parse_action_items(bank_path(store, "actionItems.md")), status, tag, owner
    )
    return {
        "scope": "workspace",
        "workspace": str(Path(workspace_root)),
        "filters": filters,
        "total": len(items),
        "items": items,
    }


def render_tasks_json(
    workspace_root: Path,
    status: str = "open",
    tag: str | None = None,
    owner: str | None = None,
    all_workspaces: bool = False,
) -> str:
    data = collect_tasks(workspace_root, status=status, tag=tag, owner=owner, all_workspaces=all_workspaces)
    return json.dumps(data, indent=2, ensure_ascii=False)


def render_tasks(
    workspace_root: Path,
    status: str = "open",
    tag: str | None = None,
    owner: str | None = None,
    all_workspaces: bool = False,
) -> str:
    def _filter(items: list[dict]) -> list[dict]:
        return _filter_items(items, status, tag, owner)

    flt = []
    if status != "all":
        flt.append(f"status={status}")
    if tag:
        flt.append(f"tag=#{tag.lstrip('#')}")
    if owner:
        flt.append(f"owner~{owner}")
    suffix = f"  [{', '.join(flt)}]" if flt else ""

    lines: list[str] = []
    if all_workspaces:
        lines.append(f"Action items — all workspaces{suffix}")
        lines.append("=" * 60)
        grand = 0
        for w in iter_workspaces():
            store = memory_home() / w["id"]
            items = _filter(_parse_action_items(bank_path(store, "actionItems.md")))
            if not items:
                continue
            grand += len(items)
            lines.append("")
            lines.append(f"# {w['root']}  ({len(items)})")
            lines.extend(_render_task_block(items))
        if grand == 0:
            lines.append("(no matching action items)")
        return "\n".join(lines) + "\n"

    store = workspace_store(workspace_root)
    items = _filter(_parse_action_items(bank_path(store, "actionItems.md")))
    lines.append(f"Action items — {Path(workspace_root).name}{suffix}")
    lines.append("=" * 60)
    if not items:
        lines.append("(no matching action items)")
        return "\n".join(lines) + "\n"
    lines.extend(_render_task_block(items))
    lines.append("")
    lines.append(f"{len(items)} item(s). Filters: --status open|done|all · --tag <t> · --owner <name> · --all")
    return "\n".join(lines) + "\n"
