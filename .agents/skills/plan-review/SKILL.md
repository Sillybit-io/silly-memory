---
name: plan-review
description: Reviews an implementation plan and answers one question. Can a developer execute it without getting stuck? Checks cited paths, startability, contradictions, QA, recorded research, product-then-technical questions, a design diagram when the request needs one, and a final verification wave of at least four gates. Reports at most three blockers with fixes, writes the verdict into the plan, and can loop up to five rounds, fixing and re-reviewing, until approved. It rejects a missing test-before-next instruction, a stop between waves, a missing todo checkbox, or a missing T0. Use when reviewing this plan, checking this plan before execution, or asking for the plan-review verdict.
license: CC-BY-ND-4.0
metadata:
  version: "0.4.0"
  category: planning
  suggested-model: openai/gpt-6-astra
  suggested-effort: max
---

# plan-review

## Purpose

plan-review is a blocker-finder, not a perfectionist. It exists to answer one question about a plan file written by `plan-writer`: can a capable developer execute it without getting stuck? It checks references, startability, contradictions, QA executability, recorded research, product-then-technical questions, a design diagram when the request needs one, and a final verification wave of at least four gates. It reports at most three blockers with a concrete fix for each, and approves when in doubt — a plan that is 80% clear is good enough. Running it on a model from a different family than the one that wrote the plan is the cheapest independent second opinion available: two models built differently tend to miss different things, where a same-family review tends to agree with itself. plan-review can also loop: on a rejection, it offers to fix the listed blockers and re-review, repeating without asking again until the plan is approved or a five-round cap is reached. Like `plan-writer`, it composes with your tool's own read-only planning mode rather than conflicting with it: its checks run the same way inside Claude Code's Plan Mode or Cursor's Plan mode, but writing `## Review` into the plan file waits for that mode's own approval step, the same as any other edit would. The `suggested-model` hint above is advisory; this skill runs on any model.

## When to use / when NOT to use

Use plan-review when you:

- Have a plan file with `status: planned` and want to know if it is safe to hand to an implementer.
- Want a second opinion from a different model than the one that wrote the plan.
- Want the loop mode to fix and re-check a plan automatically until it passes.

Do NOT use plan-review when you:

- Want a better plan, not just an executable one. Tell `plan-writer` what to change instead.
- Have no plan file yet. This skill reads a file from disk; it never reviews pasted text.
- Want the code reviewed. That is `ai-review`.
- Want the ticket refined. That is `issue-refiner`.
- Already used five rounds under the current consent and it is still rejected. The next round needs a fresh yes from the user.

## Workflow

plan-review runs in one of two modes. **Review mode** (the default) does one round, writes the verdict, and stops. **Loop mode** fixes and re-reviews without asking again until the plan is approved or the round cap is hit. Loop mode starts only when the user answers yes to the offer on a rejection, asks for it up front ("review and fix until it passes"), or a non-interactive prompt says so explicitly.

1. Take a plan file path as input. Never review a plan pasted into chat; if no path was given, ask for one. Record which mode this run is in.
2. Read the plan's frontmatter from disk. `status: draft` means the plan is not finished yet — say so and stop. `status: reviewed` means it already passed — say so and stop unless a re-review was explicitly asked for. Otherwise this round's number is the last recorded round plus one.
3. Cap check: this round would be the sixth since the round history's last `consent` row (or the first ever consent, if none exists) only when five rounds have already run without a fresh yes. When that is true, stop and ask: "Five rounds used and the plan is still rejected. Continue for up to five more, stop here so you can fix it by hand, or accept the plan as is?" In a non-interactive run, stop with the current verdict and this same question in the reply instead of guessing. A yes here adds a `consent` row to the round history and continues to step 4.
4. Check A — references. Open every `path:line` the plan cites, reading about 20 lines of context around each rather than the whole file. Budget: 40 references per round. Past that, sample the references in the first two waves' todos and say so under "Checked". A cited path that does not exist, or a line that does not support the claim, is a candidate blocker only when a todo depends on it. T0 may cite `this file`; that citation is not a broken path.
5. Check B — startability. For each todo, ask: could a developer start this now with what the plan gives? Flag a missing input, an undefined term, a dependency on an "Owner decisions pending" item that was never answered, or a dependency on a todo that does not exist.
6. Check C — contradictions. Flag two todos that disagree, a "Must have" a "Must NOT have" forbids, a dependency cycle, or a success criterion no todo produces.
7. Check D — QA executability. Flag any acceptance criterion or QA scenario that names no tool or command, gives no concrete data, or reads like "test manually" or "verify it works". A prose deliverable with a grep-for-a-sentence acceptance criterion is also a flag here.
8. Check E — UI QA. Verify the plan's `ui:` claim with the same signals plan-writer uses: a web framework in the manifest, an `index.html`, a templates or views directory, a mobile app target, or a plan that itself adds a web or mobile surface. When the project has a UI, or the plan adds one, the plan's last todo must be the automated UI QA task with a tool, a route, viewport widths, steps, and a screenshot path; its absence or vagueness is a flag.
9. Checks F through J. Read [references/writer-contract.md](references/writer-contract.md) and run research, questions, diagram, the final wave, the todo-box, test-before-next, and T0 check, and the continue-through-waves check. Budget for research sources: 5 URLs opened. Past that, say so under "Checked" and do not open more.
10. Challenge the plan's assumptions once: name the edge cases and failure modes it never mentions. Each becomes a blocker only if it would stop execution; otherwise it goes under "Notes (non-blocking)", capped at five.
11. Decide the verdict. Keep at most three blockers — the three most likely to actually stop execution — each naming the todo or section, the gap, and a concrete fix. When more than three flags exist, report in the order in `references/writer-contract.md` and add one line: "and N more of the same kind." From round 2 onward the blocker set is frozen: a later round may only report an unfixed listed blocker, a regression the fixes introduced, or a genuinely new item that would stop execution — never a new item of the kind already checked and passed. Zero blockers means `PLAN-REVIEW: OKAY`.
12. Write `## Review` in the plan file in the shape given in Output format: append this round to the history table, then write its `### Round n` body. Set `review_round` to this round's number. Set `status: reviewed` on `OKAY`; otherwise leave it `planned`.
13. On `OKAY`, in either mode: reply with the verdict block and stop. This is the only way the run ends successfully.
14. On `REJECT` in review mode: reply with the verdict block, ending it with "Fix these blockers and re-review until approved? (yes / I will fix them myself)". A yes switches to loop mode, adds a `consent` row to the round history, and continues to step 15.
15. On `REJECT` in loop mode: check whether the `plan-writer` skill is available in this session. If it is, load it and run only its fix-only follow-up step — the last step in its Workflow, never its exploration steps — against this same plan file. That step may fill a missing `## Research`, `## Questions`, `## Design`, final-wave todo, checkbox, per-todo test instruction, continue-through-waves instruction, or T0 when a blocker names it, including the research budgets that step allows. Then go to step 2 for the next round without asking again. If `plan-writer` is not available, say so plainly and stop at the verdict; loop mode cannot proceed without it.
16. On a re-review round (round 2 or later), re-verify each previously listed blocker is actually fixed, then re-run checks A through J only on the sections that changed since the last round; never re-open a section that already passed. Then walk the QA checklist.

### Handling feedback

A hedged remark about a finding — "I'm not sure blocker 2 is real" — never removes it by itself. Restate the evidence behind it, say plainly whether you would drop it if asked, and ask. A plain instruction — "drop blocker 2" — is applied directly: remove it, record it under "Notes (non-blocking)" as declined by the owner, and recompute the verdict and the round body before writing them.

## Output format

The verdict line is always exactly one of these two strings, and always the first line of the chat reply:

```text
PLAN-REVIEW: OKAY
PLAN-REVIEW: REJECT (<n> blockers)
```

Written into the plan file, replacing the `## Review` section's history table and appending a new round body (never deleting an earlier round's body):

````markdown
## Review

| Round | Date | Verdict | Blockers |
| --- | --- | --- | --- |
| 1 | <date> | REJECT | 2 |
| consent | <date> | — | user approved loop mode |
| 2 | <date> | OKAY | 0 |

### Round <n>

**Verdict:** PLAN-REVIEW: REJECT (2 blockers)

#### Blockers

1. **T3 — reference does not exist.** `src/auth/session.ts:40` is not in the tree. Fix: point at `src/auth/session-store.ts:12`, where `SessionStore` is declared, or drop the reference and restate the decision in "Do".
2. **T5 — QA scenario is not executable.** "Check that the modal looks right" names no tool, no steps, no expected result. Fix: name the browser tool, the route, the widths, the steps, and the expected `role="dialog"` state, with a screenshot path.

#### Fixed (only on a round after the first)

- Blocker 1 from round <n-1>: <what changed and where>.

#### Notes (non-blocking)

- <at most five; observations that would not stop execution>

#### Checked

- References opened: <n> of <n>; <k> broken.
- Todos startable: <n> of <n>.
- Contradictions: none | <where>.
- QA scenarios executable: <n> of <n>.
- UI claim: `ui: yes | no` verified | contradicted — <evidence>. Automated UI QA todo last: yes | no | not applicable.
- Research: present | missing — sources opened <n> of <n>; <k> broken.
- Questions: Product before Technical | wrong order — open items with neither answer nor default: <n>.
- Diagram: present | omitted — <reason> | required and missing.
- Final wave: F1–F4 present | missing <which> — UI QA last: yes | no | not applicable.
- Todo boxes: every todo starts `- [ ] Open`. The `## Todos` intro says to pass that todo's Acceptance and QA scenario, commit when it says `Commit: yes`, mark `- [x] Done`, and only then start the next todo. The final wave does not inherit that rule | missing | applied to the final wave.
- Waves: the plan says to continue through every wave until every todo and gate is done, and to fix a failed check and continue | stops between waves | missing.
- T0: first todo, copies the plan into `docs/plans/` only when it is not already there, and the build continues there | missing | not first | copies unconditionally | build stays outside.
- Previous blockers fixed: <n> of <n> | first round.
````

Reply block, review mode or the final round of loop mode:

```markdown
PLAN-REVIEW: REJECT (2 blockers) — round 1 of 5 — `docs/plans/<file>.md`
1. T3 — reference does not exist. Fix: ...
2. T5 — QA scenario is not executable. Fix: ...
Fix these blockers and re-review until approved? (yes / I will fix them myself)
```

```markdown
PLAN-REVIEW: OKAY — round <n> of 5 — `docs/plans/<file>.md` — <k> non-blocking notes recorded in the file.
```

Reply block, cap reached mid-loop:

```markdown
PLAN-REVIEW: REJECT (2 blockers) — 5 rounds used, still rejected — `docs/plans/<file>.md`
Continue for up to 5 more rounds, stop here to fix it by hand, or accept the plan as is?
```

## Guardrails

MUST:

- MUST read the plan from disk on every round; never review pasted text.
- MUST open every cited `path:line` before judging it, within the stated budget.
- MUST run all ten checks (A through J) on every round.
- MUST put the exact verdict string on the first line of every reply.
- MUST cap blockers at three per round, each naming a todo or section, a gap, and a fix.
- MUST cap non-blocking notes at five.
- MUST write `## Review`, update `review_round`, and set `status` correctly on every round.
- MUST stop and ask before a sixth round without a fresh consent.
- MUST enter loop mode only after a yes or an explicit instruction, never on its own.
- MUST freeze the blocker set after round 1: only unfixed listed blockers, regressions, or a genuinely new execution-stopping item may appear.

NEVER:

- NEVER judge architecture choice, naming, code style, or optimality. Those are not blockers.
- NEVER edit the plan file outside `## Review` and the two frontmatter keys it owns, except through `plan-writer`'s own fix-only follow-up step during the fix loop.
- NEVER fix a plan itself; only `plan-writer`, invoked explicitly, changes the plan's content.
- NEVER add a blocker to look thorough. Zero blockers is a legitimate, common outcome.
- NEVER run a round past the fifth since the last consent without asking again.
- NEVER review a `draft` plan.
- NEVER read an entire referenced file when a window around the cited line answers the question.

## QA checklist

- [ ] The plan was read from disk; the reply names its path.
- [ ] The run's mode (review or loop) is recorded and followed correctly.
- [ ] The round number is correct and no more than 5 since the last consent.
- [ ] Every cited `path:line` was opened.
- [ ] All ten checks (A–J) each produced a line under "Checked". Check J produces the todo-box line, including the test-before-next instruction, the waves line, and the T0 line.
- [ ] Blockers are capped at three, each with a todo/section, a gap, and a fix.
- [ ] Notes are capped at five.
- [ ] The verdict string is exactly `PLAN-REVIEW: OKAY` or `PLAN-REVIEW: REJECT (<n> blockers)`, and is the reply's first line.
- [ ] `## Review` was written with the round appended to the history table, never overwriting an earlier round's body.
- [ ] `review_round` and `status` match the outcome of this round.
- [ ] From round 2 on, no blocker outside the frozen set appears without being a regression or a genuinely new execution-stopping find.
- [ ] Nothing outside `## Review` and the two frontmatter keys changed, unless `plan-writer`'s fix-only follow-up step ran during a loop round.

<!-- markdownlint-disable-next-line MD034 -->
© Sillybit — https://github.com/Sillybit-io/silly-skills — CC BY-ND 4.0. Attribution required; do not republish modified versions without written approval.
