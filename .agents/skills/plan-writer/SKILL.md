---
name: plan-writer
description: Writes an implementation plan after a gap analysis. Classifies the request, researches the feature on the public web, analyzes the codebase, and asks product questions before technical ones. Sharpens a vague idea with up to three questions, then writes docs/plans/<date>-<slug>.md with that research, a design diagram when the request is a flow, and a final verification wave of at least four gates. The builder tests each todo before the next. The final wave is excluded. T0 copies the plan into the project if it is not already there. Use when writing a plan, planning a feature, planning a refactor, making an implementation plan, breaking work into tasks, or drafting a plan for review.
license: CC-BY-ND-4.0
metadata:
  version: "0.4.0"
  category: planning
  suggested-model: anthropic/claude-fable-5-1
  suggested-effort: max
---

# plan-writer

## Purpose

A plan is a record of decisions a capable developer can execute without asking anyone, not a transcript of the code to be written. plan-writer researches the feature, analyzes the codebase against that research, and asks the owner the product questions first and the technical questions second. It writes one file that survives across turns: a draft that holds the research, the analysis, and the questions, then a planned file with the task breakdown, a design diagram when the request is a flow, and a final verification wave, then a reviewed file once a second pass has checked it. It never writes code, and it never reviews its own plan — `plan-review` is the separate step that does that, on a different model so the check has different blind spots than the write. plan-writer is a skill, not your tool's own planning mode, and the two compose rather than conflict. If your session itself is in a read-only planning mode — Claude Code's Plan Mode, Cursor's Plan mode — this skill's exploration and its questions run exactly as described below, but its Write call is held by that mode's own gate: in Claude Code the write waits until you approve the proposed plan, which also switches the session out of Plan Mode; in Cursor the equivalent is the explicit build step after the plan is reviewed. A subagent wrapper carrying its own plan-only permission mode is stricter still — it denies the write outright with no approval path — which is why this skill's own agent wrappers never set one. The `suggested-model` hint above is advisory; this skill runs on any model.

## When to use / when NOT to use

Use plan-writer when you:

- Have a change that touches more than a couple of files, or carries one real design decision.
- Have only a rough idea and want it sharpened into a concrete plan in one pass.
- Need a plan another session, another model, or another person will execute.
- Want a plan that will be reviewed before anyone starts building.
- Are resuming a plan you started earlier and left as a draft.

Do NOT use plan-writer when you:

- Have a trivial change: one or two files, no design decision, no migration, no interface change. Say so and offer to do it directly.
- Want the plan reviewed. That is `plan-review`.
- Want the code written. Plan first, then build from the plan with your normal workflow.
- Want a document, not a plan. That is `tech-writing`.

## Workflow

1. Restate the request in one line. Classify its **intent** as `refactor`, `build` (from scratch), `change` (a mid-sized feature or fix), `architecture`, or `research`. Classify its **tier** as `trivial`, `standard`, or `architecture`. A `trivial` tier ends the run before research, questions, or a final wave: in an interactive session, say why in one sentence and offer to do the change directly instead of planning it; in a non-interactive run, reply `trivial: <reason>` and stop. A `standard` tier sets `review: optional` in the plan; an `architecture` tier sets `review: required`.
2. Resume check, in this order, stopping at the first hit: (a) the caller gave a plan path directly; (b) the request names a ticket id and a file under `docs/plans/` has `source: ticket:<id>` in its frontmatter; (c) the slug computed from the request (lowercase, hyphenated, 3 to 6 words) matches an existing `docs/plans/*-<slug>.md`; (d) otherwise, list every `docs/plans/*.md` file with `status: draft` and a frontmatter `date` inside the last 14 days, showing each one's `title` and `request` line, and ask which to resume, or none — in a non-interactive run, skip this and start fresh. A `draft` whose first TL;DR line starts with `Blocked:` is still blocked: say so and stop. Any other `draft` that already contains `## Research` and `## Questions` continues at step 11. Take answers from the conversation since that draft was written, or from edits already in `## Questions`, write them in, and skip steps 3 through 10. A `draft` that has neither section was not written by this workflow: say so and stop unless a new plan was explicitly asked for. A `planned` or `reviewed` match is not resumed: say so and stop unless a new plan was explicitly asked for.
3. Idea refinement. Runs only on a fresh plan (never on a resume) whose `source` is `chat` — a tracked ticket goes through `issue-refiner` instead, at step 6. Test whether the request is idea-level vague: it names no concrete deliverable, it reads as a wish or a problem area rather than a change, or who benefits and what success looks like cannot be inferred from the request or a quick look at the repository. When the request is already concrete, skip this step and say so in one line. When it is not: in an interactive session, ask up to three sharpening questions in a single message, covering whichever of these the request leaves open — who this serves and what problem it solves, what success looks like, and any real constraint or explicit non-goal — then fold the answers into the restated request before continuing. In a non-interactive run, or when the user has nothing to add, proceed anyway and record what you assumed for each open dimension under "Decisions made". This early round is one message only. The product-then-technical round in step 9 is a different round, not a second batch of these sharpening questions.
4. Determine the branch name, on a fresh plan only. Look first for a convention the repository states outright — `CONTRIBUTING.md`, `AGENTS.md`, or a pull request template naming a required prefix or pattern. When none is stated, sample the repository's own recent branches (`git branch -a --sort=-committerdate`, or `git log --all --format=%D`) for a real, observed pattern — a Conventional-Commits-style prefix, a ticket-id segment, a username segment — and match it, the same way `conventions-codifier` and `ai-review`'s standardization axis work from evidence rather than a generic style guide. When no pattern is observable either, because the repository has no branches to sample, default to `<type>/<slug>`, where `<type>` is `feat` for `build` or `change`, `fix` for a bug-shaped `change`, `refactor` for `refactor`, or `chore` for `research`, and put the ticket id ahead of the slug when `source: ticket:<id>` — `<type>/<id>-<slug>`. Record it as `branch:` in the plan's frontmatter and name it in the TL;DR, so whoever executes the plan branches the way this repository already does instead of inventing a new pattern.
5. Explore the codebase before the later question round, inside a budget. Read every file the request names, then their direct callers, their tests, their config, and any docs that describe them. Prefer searching over opening whole files. Budget: 20 files or 1,500 lines read, whichever comes first. When you hit the budget, stop exploring, write down what you have, and list the areas you did not reach under "Owner decisions pending" as `needs a look at <path>`. Record every observation as `path:line` as you go; an observation you cannot cite did not happen.
6. Run the vagueness test, but only when the request came from a tracked ticket rather than a direct ask: after exploring, there is still no stated outcome, no acceptance criterion can be written from the ticket or the code, and the scope is unbounded. When it fails this test: in an interactive session, if the `issue-refiner` skill is available, say in two lines that the ticket is too vague to plan and that issue-refiner will rewrite its description on the tracker, then ask yes or no. On yes, load `issue-refiner`, run it to completion (its own confirmation gate on the write-back still applies), then continue from step 7 using the refined brief in place of the original ticket text. On no, if issue-refiner is unavailable, or in a non-interactive run: write the draft file with its first TL;DR line reading `Blocked: ticket <id> is too vague to plan. Run issue-refiner on it, then re-run plan-writer.`, list what is missing under it, and stop. A blocked draft is not reviewed.
7. Research the feature. Read [references/external-research.md](references/external-research.md) and run the first pass. Budget: 5 searches and 5 pages. At the ceiling, write what you have and list the queries you did not run under `## Research`.
8. Deep analysis, before any later question. Write the IS/GAP ledger: `G1`, `G2`, and so on. Every IS cell cites `path:line`. A research finding goes in the GAP cell or in `## Research`, not in place of a code citation. A hidden requirement — data migration, backward compatibility, new tests, rollout, permissions, error handling — becomes a gap when the codebase or the research supports it; otherwise it is a recorded default or a question in step 9. Write the Risks table. Then run the slop check and move each hit into "Must NOT have": scope inflation, premature abstraction, over-validation, documentation bloat. Name what you cut and why. A todo that closes no gap later is scope inflation and gets cut here.
9. Ask the later round. Read [references/owner-questions.md](references/owner-questions.md). One message: product questions first, then technical questions, at most four of each. Record `## Questions` with `### Product` before `### Technical`. Do not stop in this step. In a non-interactive run, record each default and continue through step 10, including when the tier is `architecture`.
10. Write the draft file at `docs/plans/<YYYY-MM-DD>-<slug>.md` (append `-2` on a collision) with `status: draft`, the TL;DR, Scope, `## Research`, and `## Questions`. The remaining headings are present and empty. In an interactive session, reply with the draft-gate block and stop when at least one later-round question is still open, or when the tier is `architecture`. Do not stop when both groups contain only settled decisions and the tier is `standard`. Otherwise continue.
11. Second research pass. Read the second-pass section of [references/external-research.md](references/external-research.md). Skip the pass when no answer introduced a new point. Budget: 3 pages on the new point. Update the ledger and the Risks table when the pass changes them. When that section's one exception applies, write the question and its default into `## Questions` and stop. No third pass.
12. Write `## Design`. Read [references/diagram.md](references/diagram.md). Add the mermaid block when the request needs one. Otherwise write `Diagram: omitted — <reason>`.
13. Write the verification strategy: for every gap, the exact command or check that proves it closed, and the output it should produce.
14. Write the todos, each with the fields in Output format. T0 is always first: copy this plan into the project's `docs/plans/` if it is not already there. Every todo, including T0 and the final-wave gates, starts with `- [ ] Open`. For every todo under `## Todos` only, the plan tells the builder to run that todo's Acceptance and its QA scenario, pass them, make the commit when the todo says `Commit: yes`, and only then change that line to `- [x] Done` in the project copy, before the next one starts. A failure path marked n/a is not run. That test-before-next rule does not apply to the final-wave gates. Those gates still change `- [ ] Open` to `- [x] Done` when the gate is finished. If T0 copied the file, the rest of the build stays in that copy. Every acceptance criterion is something an agent can run and check: a command with its expected output, or a UI selector with its expected state, using concrete data rather than placeholders. Never write "user tests manually", "verify it works", or "check visually" as acceptance. For a prose deliverable — a document, a skill, a config file — acceptance is structural: a section exists, a validator passes, a command printed in the doc actually runs. Never a grep for a specific sentence; that pins today's wording instead of the behavior.
15. Group the implementation todos into waves by dependency: todos that share no file and have no dependency between them go in the same wave. T0 is not in a wave. It runs alone, before wave 1. Above the wave list, tell the builder to finish T0, then each wave in list order, then the final wave, without stopping to ask for a continue. Todos inside one wave stay as independent as that line says. F1–F4 stay one parallel wave. If a todo's checks fail, the builder fixes that todo and runs the checks again until they pass, then continues. Do not put that instruction inside the test-before-next paragraph. After that paragraph, add its own paragraph: when the wave is done, start the next wave, including the final wave, and do not ask for a continue. If a todo's checks fail, fix that todo and run the checks again until they pass, then continue.
16. Add the final verification wave. Read [references/final-wave.md](references/final-wave.md). Title the gates `F1`, `F2`, `F3`, and `F4`, in that order. When `ui: yes`, the last todo is the automated UI QA task.
17. Apply the transcript test to every todo: it may hold a signature, a path, or a command, and nothing longer. Replace any block of implementation code with the decision it encodes and the reference it came from. A plan that runs several times longer than the request it answers is a transcript, not a plan.
18. Update the file in place: fill every section, set `status: planned`, keep `review_round: 0`, and leave `## Review` empty for the reviewer.
19. Reply with the path and the TL;DR, then close with: "Next: (a) run plan-review on `<path>` on a different model family — required, tier is architecture / recommended otherwise; (b) start building from the plan. Which?" In a non-interactive run, state the recommendation instead of asking, and stop.
20. Follow-up after a rejection (this is the fix-only step `plan-review` runs on its own during its loop mode — see that skill): re-read the plan from disk, read the blockers listed under the latest `### Round n` in `## Review`, and fix only those plus whatever the fixes break. When a blocker names a missing `## Research`, `## Questions`, `## Design`, final-wave todo, checkbox, per-todo test instruction, continue-through-waves instruction, or T0, fill that section. Filling Research uses the budgets in [references/external-research.md](references/external-research.md). Set `review_round` to that round's number, append one line per blocker under a `#### Fixed` list for that round describing the change, keep `status: planned`, and reply with the path and a one-line summary of what changed. Never re-explore the codebase from scratch here, and never rewrite a section the blockers do not touch.

### Handling feedback

A hedged or uncertain remark from the user — "I'm not sure", "I think", "maybe", or anything ending in a question mark — never changes the plan by itself. Answer with your reasoning, the evidence behind it, a recommendation, and a yes/no question; change the plan only once the user confirms. Example: "I'm not sure T3 is needed" gets the gap T3 closes, what breaks without it, and a recommendation to keep or cut, then a question. A plain instruction is applied directly: "Remove T3" removes it, and if a gap is left uncovered as a result, say so in one line.

## Output format

The plan file, written at `docs/plans/<YYYY-MM-DD>-<slug>.md`:

````markdown
---
title: <the request, one line>
request: "<the original request, verbatim, one paragraph>"
source: chat | ticket:<id>
date: <YYYY-MM-DD>
status: draft | planned | reviewed
tier: standard | architecture
intent: refactor | build | change | architecture | research
branch: <type>/<slug> | <type>/<id>-<slug>
ui: yes | no
review: required | optional
review_round: 0
---

# <title>

## TL;DR

- Effort: S | M | L — <one reason>
- Risk: low | medium | high — <what can break>
- Decisions made: <default taken> because <reason>; ...
- Owner decisions pending: none | <question> — why only the owner can answer it; default if unanswered: <default>
- Cut from scope: <what the slop check removed, or "none">
- Branch: `<branch name>` — <the observed convention it follows, or "no existing branches to sample; used the type/slug default">

## Scope

### Affected users

<Who notices this change, and how.>

### Ideal state

<Two to four sentences describing the world after the change.>

### IS / GAP ledger

| Gap | IS today (evidence) | GAP |
| --- | --- | --- |
| G1 | `path:line` — <what exists> | <what is missing or wrong> |

### Risks

| Risk | What breaks | Mitigation | Carried by |
| --- | --- | --- | --- |
| <risk> | <consequence> | <mitigation> | T<n> |

### Must have

- <one line per item, each traceable to a gap>

### Must NOT have

- <one line per item cut by the slop check>

## Research

- Sources: <url>, or "No useful public source found."
- How others did it: <one or two lines>
- What matters: <one or two lines>
- Pros and cons: <one or two lines>
- What this plan will follow: <one line>
- Queries not run: none | <query>

## Questions

### Product

- <decision or question> — answer: <answer> | default: <default> because <reason>

### Technical

- <decision or question> — answer: <answer> | default: <default> because <reason>

## Design

<A fenced mermaid block, or one line: Diagram: omitted — <reason>>

## Verification strategy

| Gap | Proof | Expected |
| --- | --- | --- |
| G1 | `<command or check>` | `<output or state>` |

## Execution strategy

Finish T0, then each wave in the order this list gives, then the final wave. Todos inside one wave stay as independent as that wave's line says. F1–F4 stay one parallel wave. Do not stop at the end of a wave. Do not ask for a continue. If a todo's checks fail, fix that todo and run the checks again until they pass, then continue. Stop only when every todo and every gate is `- [x] Done`.

- T0: copy this plan into the project if it is not already there
- Wave 1: T1, T2 (independent)
- Wave 2: T3 (needs T1)
- Final wave: F1, F2, F3, F4

## Todos

Every todo starts with `- [ ] Open`. When you build this plan, run that todo's Acceptance and its QA scenario — the happy path, and the failure path unless it is n/a. Only after those checks pass, make this todo's commit when it says `Commit: yes`, then change that line to `- [x] Done` in the project copy. Do not start the next todo until that is done. If T0 copied the file, keep editing that copy. Do not run this test on a final-wave gate.

When the wave is done, start the next wave, including the final wave. Do not ask for a continue. If a todo's checks fail, fix that todo and run the checks again until they pass, then continue.

### T0 — Copy the plan into the project

- [ ] Open
- Do: if this file is not already inside the project, copy it to `docs/plans/` under its current name and do the rest of the build there. If it is already there, do not copy it.
- Must not: change any other part of the plan while copying, or make a second copy when it is already in the project
- Closes gap: none
- Depends on: none
- References: this file
- Acceptance: the file exists at `docs/plans/<name>.md` inside the project
- QA scenario: happy — the file is already under `docs/plans/` and there is no second copy; failure — the file is outside the project, one copy appears under `docs/plans/`, and the outside file is left as it was
- Commit: no

### T1 — <verb phrase>

- [ ] Open
- Do: <the decision, not the code>
- Must not: <the tempting wrong move>
- Closes gap: G1
- Depends on: none | T<n>
- References: `path:line`, `path:line`
- Acceptance: `<command>` prints `<expected>` | element `<selector>` shows `<state>`
- QA scenario: happy — <steps, expected>; failure — <steps, expected>
- Commit: yes — `<type>(<scope>): <subject>` | no, folds into T<n>

## Final verification wave

Change `- [ ] Open` to `- [x] Done` in the project copy when that gate is finished, before the next one starts. The test-before-next rule under `## Todos` does not apply to this wave.

### F1 — Plan compliance

- [ ] Open
- Do: confirm every Must Have exists and every Must NOT Have is absent
- Acceptance: <the check and the expected evidence>
- Commit: no

### F2 — Code quality

- [ ] Open
- Do: run the test, lint, and build commands the project defines, name any of the three it does not have, and run the slop pass
- Acceptance: each defined command exits 0
- Commit: no

### F3 — Scenario QA

- [ ] Open
- Do: execute every todo's QA scenario, including edge cases
- Acceptance: <scenarios passed, with the concrete expected results>
- Commit: no

### F4 — Scope fidelity

- [ ] Open
- Do: compare the diff to the spec
- Acceptance: no extra files, no cross-todo edits
- Commit: no

When `ui: yes`, the last todo is:

### Automated UI QA

- [ ] Open
- Do: <tool, route, viewport widths, steps with expected results, screenshot path `reports/ui-qa/<slug>/`>

When `ui: no`, omit that heading and say why in one line.

## Success criteria

| Gap | Closed by | Proof |
| --- | --- | --- |
| G1 | T1 | `<command>` → `<expected>` |

## Review

<Empty. plan-review writes here.>
````

Two reply shapes, depending on where the run stopped:

Draft-gate reply, used when step 10 stops for an open question or an `architecture` tier. List open product questions before open technical ones:

```markdown
Plan drafted: `docs/plans/<date>-<slug>.md` — tier <tier>.
<the TL;DR block verbatim>
Owner decisions needed before this plan can be filled in:
1. <product question> — <why>. Default if unanswered: <default>.
2. <technical question> — <why>. Default if unanswered: <default>.
```

Planned reply, used when step 19 completes the plan:

```markdown
Plan written: `docs/plans/<date>-<slug>.md` — tier <tier>, review <required | optional>, <n> todos in <m> waves.
<the TL;DR block verbatim>
Next: (a) run plan-review on this path on a different model family — <required | recommended>; (b) start building from the plan. Which?
```

## Guardrails

MUST:

- MUST run idea refinement before exploring when a fresh, chat-sourced request is idea-level vague, and cap that early round at three questions in one message.
- MUST explore the codebase before the product-then-technical round, within the stated budget.
- MUST research the feature within the budgets in `references/external-research.md`, and record `## Research` on every non-trivial plan.
- MUST ask product questions before technical questions, within the caps in `references/owner-questions.md`, and record both groups in `## Questions`.
- MUST cite `path:line` on every IS row and every todo reference.
- MUST derive the branch name from a convention the repository states or the repository's own recent branches before falling back to the `type/slug` default, and record it as `branch:` in the frontmatter.
- MUST keep a "Must NOT have" list, and it is never empty when the slop check found something to cut.
- MUST make every acceptance criterion something an agent can run and check, with concrete data.
- MUST write the test-before-next rule in the `## Todos` intro only: pass that todo's Acceptance and QA scenario, commit when it says `Commit: yes`, mark `- [x] Done`, and only then start the next todo. The final verification wave does not use that rule.
- MUST write, above the execution-strategy wave list, that the builder finishes T0, then each wave in list order, then the final wave, without stopping to ask for a continue. Todos inside a wave stay as independent as that line says. If a todo's checks fail, the builder fixes that todo and runs the checks again until they pass, then continues.
- MUST end the plan with `F1`, `F2`, `F3`, and `F4` in that order, and end the file with the automated UI QA todo whenever `ui: yes`.
- MUST write `## Design` with either a mermaid block or `Diagram: omitted — <reason>`.
- MUST write to `docs/plans/<date>-<slug>.md` and set `status: draft` before `status: planned`.
- MUST re-read the plan from disk on every resume or follow-up rather than trusting memory of an earlier turn.
- MUST answer a hedged remark with reasoning and a question before changing the plan.

NEVER:

- NEVER write or change code, tests, or configuration. This skill writes a plan file only.
- NEVER run `plan-review` itself, or spawn a subagent to review the plan.
- NEVER ask a second batch of idea-refinement questions. The later product-then-technical round is required and is not that second batch.
- NEVER invent a branch pattern the repository does not show any evidence of using.
- NEVER put implementation code in a todo beyond a signature, a path, or a command — the transcript test.
- NEVER write "user tests manually", "verify it works", or "check visually" as an acceptance criterion.
- NEVER write a grep-for-a-sentence acceptance criterion for a prose deliverable.
- NEVER answer an owner decision silently. It is either a recorded default with its reason, or a listed question.
- NEVER plan a change classified `trivial`.
- NEVER paste the whole plan into the chat reply; reply with the path and the TL;DR only.
- NEVER include a secret, personal data, an internal hostname, or an absolute local path in the plan file.
- NEVER commit the plan file. The user commits it.
- NEVER run a third research pass.

## QA checklist

- [ ] The reply names the intent and the tier on its first line, or the `trivial` short-circuit fired instead.
- [ ] Idea refinement ran, was explicitly skipped as already-concrete, or does not apply (a resume, or a tracked ticket) — and that early round never asked more than three questions in one message.
- [ ] `## Research` records sources and what the plan follows, or an explicit line that no useful public source was found.
- [ ] `## Questions` has `### Product` before `### Technical`, and neither group is empty.
- [ ] The branch name traces to a stated convention, an observed one, or the documented default, and appears in both the frontmatter and the TL;DR.
- [ ] Every IS row and every implementation-todo reference has `path:line`, and each was actually opened. T0 may cite this file.
- [ ] Every gap is closed by at least one implementation todo, and every implementation todo closes at least one gap. T0, F1–F4, and the UI QA todo do not need a gap.
- [ ] T0 is the first todo. It copies this plan into the project's `docs/plans/` only when the file is not already there.
- [ ] Every todo under `## Todos` and every final-wave gate starts with `- [ ] Open`. If T0 copied the file, the rest of the build stays in that copy.
- [ ] The `## Todos` intro tells the builder to pass that todo's Acceptance and QA scenario, commit when it says `Commit: yes`, mark `- [x] Done`, and only then start the next todo. The final verification wave does not use that rule. It only marks its own box when the gate is finished.
- [ ] "Must NOT have" is present, and lists anything the slop check removed.
- [ ] `## Design` is a mermaid block or `Diagram: omitted — <reason>`.
- [ ] Every acceptance criterion names a command or selector and a concrete expected result.
- [ ] No todo holds more than a signature, a path, or a command.
- [ ] The execution strategy's waves respect every "Depends on" field.
- [ ] Above the wave list, the plan tells the builder to finish each wave and start the next, including the final wave, without asking for a continue. If a todo's checks fail, the builder fixes that todo and runs the checks again until they pass, then continues. The Todos intro repeats that after the test paragraph.
- [ ] The final verification wave has `F1` through `F4` in order, and its last todo is the automated UI QA task whenever `ui: yes`, with tool, route, widths, steps, and a screenshot path.
- [ ] The frontmatter has all ten keys, with `status` matching where the run stopped.
- [ ] `## Review` is present and empty on a freshly planned file.
- [ ] The reply is the draft-gate block or the planned block, never the full plan file.
- [ ] Nothing outside `docs/plans/` was created or changed.

<!-- markdownlint-disable-next-line MD034 -->
© Sillybit — https://github.com/Sillybit-io/silly-skills — CC BY-ND 4.0. Attribution required; do not republish modified versions without written approval.
