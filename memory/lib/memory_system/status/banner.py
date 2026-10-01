"""Status banner — the colourful header memstatus prints above the dashboard.

Read this first if you are new to the codebase:
  - Detects and shows: version, embedding backend name, privacy mode,
    cached doctor integrity glyph, plus a one-time post-upgrade banner
    that gets consumed (and never shown again) on the next memstatus run.
  - Backend/version detection is wrapped in defensive try/except so a
    broken sub-system never crashes the header — it degrades to
    ``"unknown"`` instead.
  - First-run handling uses the ``markers`` sibling: the very first
    memstatus call writes ``.first-run-seen`` so subsequent calls suppress
    the welcome banner.

Public interface (imported elsewhere): ``_BANNER_BAR``, ``_BANNER_PHRASE``,
    ``render_status``, ``render_status_json``.
Depends on: system.config, paths, status.doctor_cache,
    status.main, status.markers, backends.factory (lazy in
    ``_detect_backend_name``), privacy (lazy in ``_detect_privacy_mode``).
Used by: status (package init re-export); ``bin/memory status``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from memory_system.system.config import load_config, memory_home
from memory_system.paths import cli_command, generated_rule_path, global_store, workspace_store
from memory_system.status.doctor_cache import _doctor_integrity
from memory_system.status.main import _bank_summary, _count_lines, _count_bullets, _last_event, _learning_section_lines, _mtime, _staging_breakdown
from memory_system.status.markers import _consume_upgrade_marker, _is_first_run, _read_upgrade_marker, _write_first_run_marker

_BANNER_BAR = "\u2501" * 40
_BANNER_PHRASE = "initialized and ready"


def _detect_version() -> str:
    try:
        from memory_system.system.version import installed_version
    except ImportError:
        return "unknown"
    try:
        v = installed_version(memory_home())
    except Exception:
        return "unknown"
    return str(v) if v else "unknown"


def _detect_backend_name() -> str:
    try:
        from memory_system.backends.factory import get_embedding_backend

        backend = get_embedding_backend(load_config())
        name = getattr(backend, "name", None) or backend.__class__.__name__
        return str(name)
    except Exception:
        return "unknown"


def _detect_privacy_mode() -> str:
    try:
        from memory_system.privacy import network_allowed

        return "network-allowed" if network_allowed() else "offline"
    except Exception:
        return "network-allowed" if os.environ.get("MEMORY_ALLOW_NETWORK", "0") == "1" else "offline"


def _first_run_banner_text() -> str:
    return "\n".join(
        [
            _BANNER_BAR,
            f"  ✅ silly-memory {_BANNER_PHRASE}",
            f"     Version: {_detect_version()}",
            f"     Backend: {_detect_backend_name()}",
            f"     Privacy: {_detect_privacy_mode()}",
            _BANNER_BAR,
        ]
    )


def _latest_backup_path() -> str:
    """Locate the newest numbered backup written by upgrade.sh (T23).

    Backups live next to `memory_home()` as `<home name>.upgrade-backup-NNN`
    (`~/.silly-memory.upgrade-backup-NNN` by default). Returns "(none)" if
    discovery fails or no backup is found.
    """
    try:
        home = memory_home()
    except Exception:
        return "(none)"
    try:
        candidates = [p for p in home.parent.glob(f"{home.name}.upgrade-backup-*") if p.exists()]
    except OSError:
        return "(none)"
    if not candidates:
        return "(none)"
    try:
        candidates.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        return str(candidates[-1])
    return str(candidates[-1])


def _post_upgrade_context(old_version: str) -> dict[str, str]:
    return {
        "from": old_version,
        "to": _detect_version(),
        "integrity": _doctor_integrity(),
        "backup": _latest_backup_path(),
    }


def _post_upgrade_banner_text(ctx: dict[str, str]) -> str:
    return "\n".join(
        [
            _BANNER_BAR,
            f"  ✅ Upgraded: {ctx['from']} → {ctx['to']}",
            f"     Integrity: {ctx['integrity']}",
            f"     Backup: {ctx['backup']}",
            f"     Verify: {cli_command()} doctor",
            _BANNER_BAR,
        ]
    )


def render_status(workspace_root: Path) -> str:
    cfg = load_config()
    ws = workspace_store(workspace_root)
    gs = global_store()
    rule = generated_rule_path(workspace_root)
    rule_chars = len(rule.read_text(encoding="utf-8")) if rule.exists() else 0
    cap_value = cfg.get("context_pack_token_cap", 16000)
    cap = int(cap_value) if isinstance(cap_value, (str, int, float)) else 16000

    lines: list[str] = []
    first_run = _is_first_run()
    upgrade_from = _read_upgrade_marker()
    if first_run:
        # First-run banner takes precedence (T11). Leave the upgrade marker
        # in place so the post-upgrade banner appears on the next invocation
        # — both pieces of information reach the user across two runs.
        lines.append(_first_run_banner_text())
        lines.append("")
        _ = _write_first_run_marker()
    elif upgrade_from:
        ctx = _post_upgrade_context(upgrade_from)
        lines.append(_post_upgrade_banner_text(ctx))
        lines.append("")
        _consume_upgrade_marker()
    lines.append("silly-memory — status")
    lines.append("=" * 48)
    lines.append(f"workspace : {workspace_root}")
    lines.append(f"store id  : {ws.name}")
    lines.append(f"store path: {ws}")
    lines.append("")
    lines.append("Capture (raw → derived)")
    lines.append(f"  events.jsonl      : {_count_lines(ws / 'events.jsonl')} events   (last: {_last_event(ws)})")
    lines.append(f"  observations.md   : {_count_bullets(ws / 'observations.md')} obs     (updated: {_mtime(ws / 'observations.md')})")
    staging = _staging_breakdown(ws)
    staging_total = sum(staging.values())
    detail = ", ".join(f"{k}:{v}" for k, v in sorted(staging.items())) or "none"
    lines.append(f"  staging (pending) : {staging_total} awaiting promotion ({detail})")
    lines.append("")
    lines.append("Durable memory — workspace bank")
    for name, n in _bank_summary(ws):
        lines.append(f"  {name:<22}: {n} facts")
    lines.append("")
    lines.append("Durable memory — global bank")
    for name, n in _bank_summary(gs):
        lines.append(f"  {name:<22}: {n} facts")
    lines.append("")
    lines.append("Injected context pack (loaded every chat)")
    status = "ok" if rule_chars else "MISSING (renders on next session)"
    approx_tok = rule_chars // 4
    lines.append(f"  rule file : {rule}")
    lines.append(f"  size      : {rule_chars} chars (~{approx_tok} tok / cap {cap}) [{status}]")
    lines.append(f"  updated   : {_mtime(rule)}")
    lines.append("")
    lines.append("Tips: `memory recall \"<query>\"` to search · `memory show <view>` to inspect")
    lines.extend(_learning_section_lines(ws))
    return "\n".join(lines) + "\n"


def render_status_json(workspace_root: Path) -> str:
    first_run = _is_first_run()
    upgraded_from = _read_upgrade_marker()

    ws = workspace_store(workspace_root)
    payload: dict[str, object] = {
        "workspace": str(Path(workspace_root)),
        "workspace_id": ws.name,
        "first_run": first_run,
        "version": _detect_version(),
        "backend": _detect_backend_name(),
        "privacy": _detect_privacy_mode(),
    }
    if upgraded_from is not None:
        payload["upgraded_from"] = upgraded_from
        payload["upgrade"] = _post_upgrade_context(upgraded_from)
        _consume_upgrade_marker()
    if first_run:
        _ = _write_first_run_marker()
    return json.dumps(payload, indent=2, ensure_ascii=False)
