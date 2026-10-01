## Prompt

> Review `docs/plans/2026-09-26-add-release-notes-skill.md` and fix it until it passes.

This is the planned file from `plan-writer`'s own `examples.md`, with two defects introduced by a hand-edit between writing and review: T3's References line now cites `scripts/validate.ts:452` for the badge check instead of the real line, and T2's acceptance criterion has been loosened to "make sure examples.md reads well."

## Without skill

An agent with no plan-review reads the plan and returns a general critique instead of a verdict: it praises the structure, suggests reordering T3 ahead of T2 for no stated reason, proposes adding back the `bun run release-notes` script the plan explicitly cut, and mentions the wrong line number in passing among eight bullet points without flagging it as something that blocks execution. It never notices that T2's acceptance criterion is not something a second agent could check. No verdict line is produced, and the plan file is never touched.

## With skill

**Round 1.** The prompt asks for the fix-and-re-review loop up front, so this run starts in loop mode.

**Step 1–2.** Input is the plan path. `status: planned`, so the review can proceed. No prior `## Review` section exists, so this is round 1.

**Step 3.** Not applicable yet; the cap only matters from round 6 onward.

**Step 4, check A.** Opens every cited path. `scripts/validate.ts:452` does not contain `checkBadgeCount` — the real function starts several lines later. Because T3 depends on that citation to justify updating the badge, this is a candidate blocker.

**Step 5, check B.** Every todo names a clear starting point except T2, whose acceptance criterion gives no command or check a second agent could run.

**Step 6, check C.** No contradictions between "Must have" and "Must NOT have"; no dependency cycle.

**Step 7, check D.** T2's "make sure examples.md reads well" names no tool, no command, and no concrete expected result — flagged.

**Step 8, check E.** `ui: no` is stated and matches the evidence: the manifest defines no web framework and there is no `index.html`, templates directory, or mobile target, and the plan does not add one. The final wave correctly has no UI QA todo. Verified, not a blocker.

**Step 9, checks F through I.** Research cites Keep a Changelog and Semantic Versioning. Both URLs were opened: [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) describes Added, Changed, and Fixed, and [Semantic Versioning](https://semver.org/) describes major, minor, and patch. The pages support "what this plan will follow." `## Questions` has Product before Technical, and every item has an answer. `## Design` is `Diagram: omitted — the request is a list of files to add, not a process, a user journey, or a system interaction.` The request is a file list, so the omission is correct. F1 through F4 are present in order, and F3 names no browser tool. Not blockers.

**Step 10.** No further edge case would stop execution; nothing added to Notes this round.

**Step 11.** Two blockers survive, both real:

1. T3's reference does not exist at the cited line.
2. T2's acceptance criterion is not executable.

**Step 12.** Writes `## Review` into the plan file.

```markdown
## Review

| Round | Date | Verdict | Blockers |
| --- | --- | --- | --- |
| 1 | 2026-09-26 | REJECT | 2 |

### Round 1

**Verdict:** PLAN-REVIEW: REJECT (2 blockers)

#### Blockers

1. **T3 — reference does not exist.** `scripts/validate.ts:452` is not where `checkBadgeCount` is declared. Fix: cite the line where the function is actually defined.
2. **T2 — acceptance criterion is not executable.** "Make sure examples.md reads well" names no command and no expected result. Fix: restore "`bun run validate` reports no `MISSING_EXAMPLES` error for `skills/workflow/release-notes/`".

#### Notes (non-blocking)

- None this round.

#### Checked

- References opened: 9 of 9; 1 broken.
- Todos startable: 8 of 9 (T2 blocked).
- Contradictions: none.
- QA scenarios executable: 8 of 9.
- UI claim: `ui: no` verified — no web framework, `index.html`, templates directory, or mobile target found, and the plan adds none. Automated UI QA todo last: not applicable.
- Research: present — sources opened 2 of 2; 0 broken. Opened `https://keepachangelog.com/en/1.1.0/` and `https://semver.org/`.
- Questions: Product before Technical — open items with neither answer nor default: 0.
- Diagram: omitted — the request is a list of files to add, not a process, a user journey, or a system interaction.
- Final wave: F1–F4 present — UI QA last: not applicable.
- Todo boxes: every todo starts `- [ ] Open`. The `## Todos` intro says to pass that todo's Acceptance and QA scenario, commit when it says `Commit: yes`, mark `- [x] Done`, and only then start the next todo. The final wave does not inherit that rule.
- Waves: the plan says to continue through every wave until every todo and gate is done, and to fix a failed check and continue.
- T0: first todo, copies the plan into `docs/plans/` only when it is not already there, and the build continues there.
- Previous blockers fixed: first round.
```

Chat reply, ending with the fix-now offer since this is round 1 in review-mode terms, immediately answered by the up-front loop request:

```markdown
PLAN-REVIEW: REJECT (2 blockers) — round 1 of 5 — `docs/plans/2026-09-26-add-release-notes-skill.md`
1. T3 — reference does not exist. Fix: cite the line where `checkBadgeCount` is actually defined.
2. T2 — acceptance criterion is not executable. Fix: restore a `bun run validate` check for `MISSING_EXAMPLES`.
Fix these blockers and re-review until approved? (yes / I will fix them myself)
```

**Step 14.** Because the original prompt already asked for "fix it until it passes," this counts as an up-front loop instruction: loop mode is confirmed and a `consent` row is added to the round history without waiting for a further answer.

**Step 15.** `plan-writer` is available in this session. It is loaded and run only through its fix-only follow-up step: it re-reads the plan from disk, fixes exactly the two listed blockers (corrects the T3 reference, restores T2's acceptance criterion), leaves every other section untouched, and appends a `#### Fixed` list to round 1 before plan-review's next pass reads it.

**Round 2.** plan-review re-reads the plan from disk.

**Step 16.** Verifies both round-1 blockers are fixed: T3 now cites the correct line and it does support the claim; T2's acceptance criterion is now a runnable command. Re-runs checks A–J only on T2 and T3, the sections that changed; does not re-open T1, T4, or F1–F4, which already passed. No regression found. No new execution-stopping item found.

**Step 11 (round 2).** Zero blockers.

```markdown
## Review

| Round | Date | Verdict | Blockers |
| --- | --- | --- | --- |
| 1 | 2026-09-26 | REJECT | 2 |
| consent | 2026-09-26 | — | user approved loop mode |
| 2 | 2026-09-26 | OKAY | 0 |

### Round 1

**Verdict:** PLAN-REVIEW: REJECT (2 blockers)

#### Blockers

1. **T3 — reference does not exist.** `scripts/validate.ts:452` is not where `checkBadgeCount` is declared. Fix: cite the line where the function is actually defined.
2. **T2 — acceptance criterion is not executable.** "Make sure examples.md reads well" names no command and no expected result. Fix: restore "`bun run validate` reports no `MISSING_EXAMPLES` error for `skills/workflow/release-notes/`".

#### Notes (non-blocking)

- None this round.

#### Checked

- References opened: 9 of 9; 1 broken.
- Todos startable: 8 of 9 (T2 blocked).
- Contradictions: none.
- QA scenarios executable: 8 of 9.
- UI claim: `ui: no` verified. Automated UI QA todo last: not applicable.
- Research: present — sources opened 2 of 2; 0 broken. Opened `https://keepachangelog.com/en/1.1.0/` and `https://semver.org/`.
- Questions: Product before Technical — open items with neither answer nor default: 0.
- Diagram: omitted — the request is a list of files to add, not a process, a user journey, or a system interaction.
- Final wave: F1–F4 present — UI QA last: not applicable.
- Todo boxes: every todo starts `- [ ] Open`. The `## Todos` intro says to pass that todo's Acceptance and QA scenario, commit when it says `Commit: yes`, mark `- [x] Done`, and only then start the next todo. The final wave does not inherit that rule.
- Waves: the plan says to continue through every wave until every todo and gate is done, and to fix a failed check and continue.
- T0: first todo, copies the plan into `docs/plans/` only when it is not already there, and the build continues there.
- Previous blockers fixed: first round.

### Round 2

**Verdict:** PLAN-REVIEW: OKAY

#### Blockers

None.

#### Fixed

- Blocker 1 from round 1: T3 now cites the line where `checkBadgeCount` is actually declared, and the line supports the claim.
- Blocker 2 from round 1: T2's acceptance criterion is now "`bun run validate` reports no `MISSING_EXAMPLES` error for `skills/workflow/release-notes/`", which a second agent can run.

#### Notes (non-blocking)

- None.

#### Checked

- References opened: 2 of 2 (only the changed sections); 0 broken.
- Todos startable: 9 of 9.
- Contradictions: none.
- QA scenarios executable: 9 of 9.
- UI claim: `ui: no`, unchanged, not re-verified this round.
- Research: unchanged, not re-opened this round.
- Questions: unchanged, not re-opened this round.
- Diagram: omitted, unchanged, not re-opened this round.
- Final wave: F1–F4 unchanged, not re-opened this round.
- Todo boxes: unchanged, not re-opened this round.
- Waves: unchanged, not re-opened this round.
- T0: unchanged, not re-opened this round.
- Previous blockers fixed: 2 of 2.
```

Chat reply for round 2:

```markdown
PLAN-REVIEW: OKAY — round 2 of 5 — `docs/plans/2026-09-26-add-release-notes-skill.md` — 0 non-blocking notes recorded in the file.
```

The plan's frontmatter now reads `status: reviewed`, `review_round: 2`, and the loop stops here — the only way a loop-mode run ends successfully.

### If the loop had not converged

Had T3's underlying problem been unfixable — say, the file it needed to reference did not exist anywhere in the repository and could not be created within scope — round 5 would still end in `PLAN-REVIEW: REJECT`, and the round-history table would show five `REJECT` rows under the same `consent` row rather than a sixth attempt:

```markdown
| Round | Date | Verdict | Blockers |
| --- | --- | --- | --- |
| 1 | 2026-09-26 | REJECT | 2 |
| consent | 2026-09-26 | — | user approved loop mode |
| 2 | 2026-09-26 | REJECT | 1 |
| 3 | 2026-09-26 | REJECT | 1 |
| 4 | 2026-09-26 | REJECT | 1 |
| 5 | 2026-09-26 | REJECT | 1 |
```

```markdown
PLAN-REVIEW: REJECT (1 blockers) — 5 rounds used, still rejected — `docs/plans/2026-09-26-add-release-notes-skill.md`
Continue for up to 5 more rounds, stop here to fix it by hand, or accept the plan as is?
```

The loop never runs a sixth round on its own; it stops and hands the decision back.
