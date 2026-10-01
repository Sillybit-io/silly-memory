---
name: update-memory-bank
description: Reconcile the entire memory bank against the current workspace state. Use when the user says "update memory bank", "refresh memories", "sync the bank", or after significant project changes.
---

# Update memory bank

## When to use
- User explicitly asks to refresh / sync / reconcile the bank.
- After a significant workspace change (new folder, restructure).

## Instructions
1. Resolve stores: workspace `~/.silly-memory/<workspace-id>/memory-bank/` and global `~/.silly-memory/_global/memory-bank/` (see `.silly-memory/memory-id`). If missing, run /init-memory first.
2. Read every file in both memory-bank directories (workspace + global as applicable).
3. Walk the workspace top-level directories and diff against what's recorded.
4. If `meetingNotes/` exists, reconcile the notes index:
   - Read `meetingNotes/README.md` if present (if absent, propose creating it).
   - Diff indexed notes against real files under `meetingNotes/**`.
   - Propose index updates for new/changed/empty/removed notes (do not apply yet).
   - Distill durable facts from newly discovered notes into canonical memory-bank bullets.
5. Reconcile action items (when `meetingNotes/` exists or the session surfaced tasks):
   - Read `memory-bank/actionItems.md` if present (if absent, propose creating it per `memory-bank/README.md` Action items format).
   - Extract from meeting-note **Action Items** tables, follow-up checklists (`- [ ]` / `- [x]`), and Director/Leadership commitment lines that assign work.
   - Normalize owner names in line with meetingNotes conventions (for example `Constantine` -> `Constantin`).
   - Diff against existing entries: propose new `- [ ]` open lines; propose flipping to `- [x]` done (add `done: YYYY-MM-DD`) when the source note marks complete or the user confirms.
   - Do not invent tasks; do not auto-delete lines — defer removal to `/remove-memory` or `/prune-memory`.
   - **Optional Jira link (READ-ONLY):** if the Atlassian MCP is available, link open items to Jira. Only query items that are not yet linked (no `jira:` field) or currently marked `jira: none` — never re-query items already linked to a key. For each such item, search Jira read-only (`searchJiraIssuesUsingJql` via `CallMcpTool`) using task keywords + normalized owner, **always restricted to the last ~6 months** (append `AND updated >= -26w` to the JQL). Matching is fuzzy (action items rarely carry a key), so propose the single best-match ticket and only annotate after the user confirms; appending `; jira: <KEY> (<status>)` to the line. If no ticket is found, append `; jira: none` (greppable via `rg 'jira: none'`). NEVER create, update, comment on, or transition Jira issues — those are ask-first writes and out of scope for this skill. If the Atlassian MCP errors with an auth/authorization failure, say so (suggest `mcp_auth`) and skip this sub-step.
   - **Optional Confluence link (READ-ONLY):** if the Atlassian MCP is available, link items that reference a Confluence page to that page. Search read-only (`searchConfluenceUsingCql` via `CallMcpTool`) scoped by `title ~ "..."` within a likely space (do NOT use broad full-text `text ~` matching — too noisy), **always restricted to the last ~6 months** (append `AND lastmodified >= now("-26w")` to the CQL). Propose the single best-match page and only annotate after the user confirms; append `; confluence: <title> (<url>)`. If none found, append `; confluence: none`. **READ-ONLY HARD RULE:** NEVER create, edit, update, comment on, or publish Confluence — `createConfluencePage`, `updateConfluencePage`, `createConfluenceFooterComment`, `createConfluenceInlineComment` (and equivalents) are forbidden. Only read/search tools are allowed. Action items that ask to *create* a page stay manual/ask-first; this step only links pages that already exist.
6. For each discrepancy or new fact, propose specific canonical-format bullets grouped by destination file:

       - [TODAY] #tag1 #tag2: <fact>.

   Use today's date in `YYYY-MM-DD`.
7. Ask the user to confirm before applying. Apply only confirmed bullets, index changes, and action-item updates.
8. Do not delete memory entries during a reconcile — defer deletions to `/remove-memory` or `/prune-memory`.
9. Summarize: bullets added, index updates, action items added / marked done, Jira links annotated (read-only), files touched.
10. Run `python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" render --workspace .` and `python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" reindex --workspace .` after applying changes.
