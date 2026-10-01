## Prompt

> Add a release-notes skill to this repository. It should turn merged pull requests into a human-focused changelog and recommend the SemVer bump that goes with it.

This is the real Roadmap entry at `README.md:83` in this repository, used as-is as a real feature request.

## Without skill

An agent with no plan-writer still produces a reasonable-looking task list, but it is a to-do list, not a plan: no exploration is shown, no category decision is made explicit, and the acceptance criteria are not things another agent could check.

```markdown
Plan for the release-notes skill:

1. Create `skills/workflow/release-notes/SKILL.md` with the usual sections.
2. Write `examples.md` next to it.
3. Update the README to list the new skill and bump the badge.
4. Add a CHANGELOG entry.
5. Run `bun run validate` to make sure it passes.

I'll have the skill read merged PRs (probably with `gh pr list --state merged`) and
turn them into a changelog section, then suggest whether the release should be a
patch, minor, or major bump based on the PR titles.
```

This baseline never opens `skill-writer/SKILL.md` to check the six-section contract, never notices that `scripts/validate.ts` enforces the README's skill-count badge as an exact match, and its one acceptance criterion — "make sure it passes" — is not something a second agent could run without guessing which command and which exit code count as success. It also picks the `workflow` category without saying why, and it never asks whether the skill should write the changelog entry into `CHANGELOG.md` directly or only print it, even though that choice affects a file the repository's own release tooling parses.

## With skill

**Step 1 — restate and classify.** "Add a release-notes skill that turns merged pull requests into a changelog and a SemVer recommendation." Intent: `build`. Tier: `standard` — four files touched (`SKILL.md`, `examples.md`, `README.md`, `CHANGELOG.md`), one real design decision (how the skill sources merged pull requests and what it does with the changelog entry), no migration, no public interface change outside this repository. `review: optional`.

**Step 2 — resume check.** No `docs/plans/*release-notes*` file exists yet. Start fresh.

**Step 3 — idea refinement.** The request already names a concrete deliverable (a skill that turns merged pull requests into a changelog and a bump recommendation), so it passes the idea-vagueness test. Skipped; noted in one line and moved on.

**Step 4 — branch name.** `CONTRIBUTING.md` states no branch convention. Sampling this repository's own recent branches (`git branch -a`) shows two real patterns: `feat/<slug>` and `<username>/<ticket-id>-<slug>`. This request has no tracked ticket, so the `<username>/...` pattern does not apply; the observed `feat/<slug>` pattern does. Branch: `feat/add-release-notes-skill`.

**Step 5 — explore.** Reads, in order: `README.md` (the roadmap line at `:83`, the Skills table, the badge line), `skills/workflow/skill-writer/SKILL.md` (the category list at `:35`, the six-section contract), `scripts/validate.ts` (`CATEGORIES` at `:83-89`, `checkBadgeCount` at `:463`), `CHANGELOG.md` (the `## [Unreleased]` heading at `:7`), and `skills/workflow/issue-refiner/SKILL.md` for the shape of a "preference order" table, since release-notes will need one for how it sources pull requests. 12 files read, under budget.

**Step 6 — ticket vagueness.** Not applicable; this came as a direct request, not a tracked ticket.

**Step 7 — research.** Queries come from the request: a human-focused changelog generated from merged pull requests, plus a SemVer recommendation. Two pages opened, under the budget of 5 searches and 5 pages. [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) groups entries as Added, Changed, and Fixed and tells the writer to record changes for people, not as a commit dump. [Semantic Versioning](https://semver.org/) maps a breaking change to major, a backward-compatible feature to minor, and a fix to patch. What this plan will follow: print a Keep a Changelog block and a SemVer recommendation, and do not invent a second grouping scheme. Queries not run: none.

**Step 8 — IS/GAP ledger, risks, and the slop check.**

| Gap | IS today (evidence) | GAP |
| --- | --- | --- |
| G1 | `README.md:83` lists `release-notes` as a Roadmap bullet with no implementation | No `skills/*/release-notes/SKILL.md` exists |
| G2 | `skill-writer/SKILL.md:35` requires one of six categories and the fixed six-section body | The new skill must pick a category and follow the contract exactly |
| G3 | `scripts/validate.ts:463` (`checkBadgeCount`) fails the build unless the README badge equals the discovered skill count | Adding a skill without updating the badge and the Roadmap line breaks `bun run validate` |
| G4 | `CHANGELOG.md:7` (`## [Unreleased]`) has no entry for this skill | A new skill needs a `### Added` line in the same change, per `CONTRIBUTING.md` |

Hidden requirements and risks, still in step 8:

| Risk | What breaks | Mitigation | Carried by |
| --- | --- | --- | --- |
| Description over 700 characters | Costs tokens in every session's system prompt, against the house rule this repository now follows | Keep the description under 700 characters and verify with a length check | T1 |
| Badge count drifts from the real skill count | `bun run validate` fails with `BADGE_COUNT` | Bump the badge in the same commit that adds the skill | T3 |

The category defaults to `workflow` during the slop check — it produces a repository artifact from git history the way `conventions-codifier` does, not a review of a diff the way the `review` skills do. The slop check cuts one item a first draft might add: a new `bun run release-notes` package script — the request never asked for a script, only a skill.

**Step 9 — product questions, then technical.** Product is already settled by the request: the person cutting the release, success is a changelog block plus a bump recommendation, and a package script is out of scope. One technical question is still open: **"Should release-notes write its entry directly into `CHANGELOG.md`, or only print it for the user to paste?"** Writing directly touches a file `scripts/prepare-release.ts` also parses, so a wrong default could corrupt a release. Default if unanswered: print only.

**Step 10 — draft file and gate.** Because one technical question is open, plan-writer writes the draft — TL;DR, Scope, Research, and Questions — and stops.

```markdown
Plan drafted: `docs/plans/2026-09-26-add-release-notes-skill.md` — tier standard.
- Effort: M — four files, one design decision, no migration
- Risk: low — additive change, existing skills and validator checks are unaffected
- Decisions made: category is `workflow` because the skill produces a repository artifact from git history, not a diff review
- Owner decisions pending: should release-notes write into `CHANGELOG.md` directly, or only print the entry? Writing directly touches a file `scripts/prepare-release.ts` also parses. Default if unanswered: print only.
- Cut from scope: a `bun run release-notes` package script — not requested

Owner decisions needed before this plan can be filled in:
1. Write into `CHANGELOG.md` directly, or print the entry only? — only the owner can accept the risk of an automated edit to a file the release tooling parses. Default if unanswered: print only.
```

The open question in the reply is the technical one. The product decisions are already settled, so the draft records them under `### Product` and the reply does not ask them again.

**After the answer ("print only, for now").** plan-writer resumes at step 11. The answer confirms the recorded default and introduces no new technology or constraint, so the second research pass does not run. `ui: no` — this is a Bun CLI repository with no web or mobile surface, and this plan does not add one, so the final verification wave has no UI QA todo. The request is a list of files to add, not a process or a user journey, so `## Design` records the omission.

```markdown
---
title: Add a release-notes skill
request: "Add a release-notes skill to this repository. It should turn merged pull requests into a human-focused changelog and recommend the SemVer bump that goes with it."
source: chat
date: 2026-09-26
status: planned
tier: standard
intent: build
branch: feat/add-release-notes-skill
ui: no
review: optional
review_round: 0
---

# Add a release-notes skill

## TL;DR

- Effort: M — four files, one design decision, no migration
- Risk: low — additive change, existing skills and validator checks are unaffected
- Decisions made: category is `workflow` because the skill produces a repository artifact from git history, not a diff review; pull-request source order follows `issue-refiner`'s preference-table pattern (`gh` first, `glab` second, `git log <tag>..HEAD` as a fallback)
- Owner decisions pending: none — write into `CHANGELOG.md` directly, or print only? Answered: print only
- Cut from scope: a `bun run release-notes` package script — not requested
- Branch: `feat/add-release-notes-skill` — follows this repository's own observed `feat/<slug>` pattern; no tracked ticket, so the `<username>/<ticket-id>-<slug>` pattern seen elsewhere does not apply

## Scope

### Affected users

Maintainers cutting a release, who currently write the changelog and pick the SemVer bump by hand.

### Ideal state

Running the skill against a range of merged pull requests produces a ready-to-paste `### Added` / `### Changed` / `### Fixed` block and a recommended bump, without touching `CHANGELOG.md` itself.

### IS / GAP ledger

| Gap | IS today (evidence) | GAP |
| --- | --- | --- |
| G1 | `README.md:83` lists `release-notes` as a Roadmap bullet with no implementation | No `skills/workflow/release-notes/SKILL.md` exists |
| G2 | `skill-writer/SKILL.md:35` requires one of six categories and the fixed six-section body | The new skill must follow the contract exactly |
| G3 | `scripts/validate.ts:463` fails the build unless the README badge equals the discovered skill count | The badge and the Roadmap line need updating in the same change |
| G4 | `CHANGELOG.md:7` has no entry for this skill | A `### Added` line is required in the same change |

### Risks

| Risk | What breaks | Mitigation | Carried by |
| --- | --- | --- | --- |
| Description over 700 characters | Costs tokens in every session's system prompt | Keep the description under 700 characters and check its length | T1 |
| Badge count drifts from the real skill count | `bun run validate` fails with `BADGE_COUNT` | Bump the badge in the same commit that adds the skill | T3 |

### Must have

- A `skills/workflow/release-notes/SKILL.md` and `examples.md` that pass `bun run validate` (G1, G2)
- The README table, badge, and Roadmap updated in the same change (G3)
- A `CHANGELOG.md` entry in the same change (G4)

### Must NOT have

- A new `bun run release-notes` package script — not requested
- Any write to `CHANGELOG.md` from the skill itself — the owner chose print-only

## Research

- Sources: [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), [Semantic Versioning 2.0.0](https://semver.org/)
- How others did it: Keep a Changelog groups a release into Added, Changed, and Fixed, written for people. SemVer picks major, minor, or patch from whether the change breaks callers, adds a compatible feature, or only fixes.
- What matters: one grouping scheme, and a bump rule that follows the change rather than the pull request count.
- Pros and cons: printing the entry keeps the release tooling's parse of `CHANGELOG.md` intact; writing the file directly is faster and can corrupt that parse.
- What this plan will follow: print a Keep a Changelog block and a SemVer recommendation.
- Queries not run: none

## Questions

### Product

- Who it is for — answer: the person cutting the release. The request already names a human-focused changelog.
- What success looks like — answer: a ready-to-paste changelog block and a recommended bump.
- Out of scope — answer: a package script. The request asked for a skill, not a script.

### Technical

- Write the entry into `CHANGELOG.md`, or print it only? — answer: print only. Writing the file touches a file `scripts/prepare-release.ts` also parses.

## Design

Diagram: omitted — the request is a list of files to add, not a process, a user journey, or a system interaction.

## Verification strategy

| Gap | Proof | Expected |
| --- | --- | --- |
| G1 | `bun run validate` | reports one more skill than before, 0 errors |
| G2 | manual read of `SKILL.md` | exactly six H2 sections in the fixed order, footer present |
| G3 | `bun run validate` | no `BADGE_COUNT` error |
| G4 | `grep -c 'release-notes' CHANGELOG.md` | at least 1 |

## Execution strategy

Finish T0, then each wave in the order this list gives, then the final wave. Todos inside one wave stay as independent as that wave's line says. F1–F4 stay one parallel wave. Do not stop at the end of a wave. Do not ask for a continue. If a todo's checks fail, fix that todo and run the checks again until they pass, then continue. Stop only when every todo and every gate is `- [x] Done`.

- T0: copy this plan into the project if it is not already there
- Wave 1: T1, T2 (independent)
- Wave 2: T3 (needs T1, to know the final skill name and description)
- Wave 3: T4 (needs T3, to reference the final line numbers)
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
- Acceptance: the file exists at `docs/plans/2026-09-26-add-release-notes-skill.md` inside the project
- QA scenario: happy — the file is already under `docs/plans/` and there is no second copy; failure — the file is outside the project, one copy appears under `docs/plans/`, and the outside file is left as it was
- Commit: no

### T1 — Write the release-notes SKILL.md

- [ ] Open
- Do: write `skills/workflow/release-notes/SKILL.md` following `skill-writer`'s six-section contract; source pull requests with the preference order `gh pr list --state merged` first, `glab mr list --merged` second, `git log <last-tag>..HEAD --oneline` as a fallback; keep the description under 700 characters
- Must not: write into `CHANGELOG.md`; the skill only prints the entry
- Closes gap: G1, G2
- Depends on: none
- References: `skills/workflow/skill-writer/SKILL.md:35`, `skills/workflow/issue-refiner/SKILL.md` (preference-table pattern)
- Acceptance: `bun run validate` reports no `LAYOUT`, `FRONTMATTER`, `NAME_MISMATCH`, or `FOOTER` error for this file
- QA scenario: happy — run the skill against this repository's own last five merged pull requests, expect a changelog block and a bump recommendation; failure — run it with no merged pull requests in range, expect it to say so and print nothing
- Commit: no, folds into the wave commit

### T2 — Write examples.md

- [ ] Open
- Do: write `skills/workflow/release-notes/examples.md` with a real scenario built from this repository's own merged pull requests
- Must not: invent pull request titles that were not actually merged
- Closes gap: G1
- Depends on: T1
- References: `skills/workflow/skill-writer/SKILL.md` (`### examples.md` rules)
- Acceptance: `bun run validate` reports no `MISSING_EXAMPLES` error for `skills/workflow/release-notes/`
- QA scenario: happy — the three required headings appear in order; failure — a temporary blank `examples.md` reproduces `MISSING_EXAMPLES`, then is restored
- Commit: no, folds into the wave commit

### T3 — Update the README

- [ ] Open
- Do: bump the skill-count badge by one, add a `workflow | release-notes` row to the Skills table, remove the Roadmap bullet at `README.md:83`
- Must not: touch any other Roadmap entry
- Closes gap: G3
- Depends on: T1
- References: `README.md:83`, `scripts/validate.ts:463`
- Acceptance: `bun run validate` reports no `BADGE_COUNT` error
- QA scenario: happy — the badge, the table row, and the Roadmap removal are all present; failure — leaving the badge unbumped reproduces `BADGE_COUNT`, then is fixed
- Commit: no, folds into the wave commit

### T4 — Add the CHANGELOG entry

- [ ] Open
- Do: add one `### Added` line under `## [Unreleased]` naming the new skill in the same one-line style as the existing 0.1.0 entries
- Must not: create a new release section; this stays under `[Unreleased]`
- Closes gap: G4
- Depends on: T3
- References: `CHANGELOG.md:7`
- Acceptance: `grep -c 'release-notes' CHANGELOG.md` is at least 1
- QA scenario: happy — the line appears under `### Added`; failure — n/a, this is a documentation-only edit
- Commit: yes — `feat(workflow): add release-notes skill`

## Final verification wave

Change `- [ ] Open` to `- [x] Done` in the project copy when that gate is finished, before the next one starts. The test-before-next rule under `## Todos` does not apply to this wave.

### F1 — Plan compliance

- [ ] Open
- Do: confirm the skill file, `examples.md`, the README row, and the changelog line exist, and that no `bun run release-notes` script was added
- Acceptance: `bun run validate` reports the new skill and no `BADGE_COUNT` error, and `package.json` has no `release-notes` script
- Commit: no

### F2 — Code quality

- [ ] Open
- Do: run `bun run validate` and `bun test`. This repository defines no separate build command. Then scan the new skill for an empty code block left as a placeholder and for a description over 700 characters.
- Acceptance: both commands exit 0, and the description is under 700 characters
- Commit: no

### F3 — Scenario QA

- [ ] Open
- Do: execute the happy and failure QA scenario on T1, T2, T3, and T4
- Acceptance: the changelog block and bump recommendation print, an empty pull-request range prints nothing, and a missing `examples.md` reproduces `MISSING_EXAMPLES` before it is restored
- Commit: no

### F4 — Scope fidelity

- [ ] Open
- Do: compare the diff with T1 through T4
- Acceptance: the diff touches only `skills/workflow/release-notes/`, `README.md`, and `CHANGELOG.md`
- Commit: no

`ui: no` — this repository is a Bun CLI and a markdown catalogue with no web or mobile surface, and this plan adds none, so no automated UI QA todo is added.

## Success criteria

| Gap | Closed by | Proof |
| --- | --- | --- |
| G1 | T1, T2 | `bun run validate` reports the new skill, 0 errors |
| G2 | T1 | manual read confirms the six-section contract |
| G3 | T3 | `bun run validate` reports no `BADGE_COUNT` error |
| G4 | T4 | `CHANGELOG.md` names the skill under `[Unreleased]` |

## Review

```

```markdown
Plan written: `docs/plans/2026-09-26-add-release-notes-skill.md` — tier standard, review optional, 9 todos in 4 waves, T0 first.
- Effort: M — four files, one design decision, no migration
- Risk: low — additive change, existing skills and validator checks are unaffected
- Decisions made: category is `workflow`; pull-request source order follows `issue-refiner`'s preference-table pattern
- Owner decisions pending: none
- Cut from scope: a `bun run release-notes` package script — not requested
- Branch: `feat/add-release-notes-skill`
Next: (a) run plan-review on this path on a different model family — recommended; (b) start building from the plan. Which?
```

```text
Not part of the plan file above. This is the shape when the request is a flow.

flowchart TD
  merged[Merged pull requests]
  skill[release-notes skill]
  printed[Printed changelog and bump]
  merged --> skill --> printed
```

### When idea refinement actually fires

The prompt above already names a concrete deliverable, so step 3 skips straight past idea refinement. A fuzzier version of the same request shows the other branch: "We should make it easier for maintainers to see what changed before they cut a release." This states a problem area, not a change, and neither who benefits precisely nor what "easier" means can be inferred from the request alone — it fails the idea-vagueness test.

```markdown
Before I explore the codebase, three quick questions:
1. Who hits this most — the person cutting the release, or reviewers checking what shipped?
2. What would "easier" look like when it's done: a generated changelog draft, a dashboard, something else?
3. Any constraint I should design around — must it avoid touching CHANGELOG.md directly, must it need no new dependency, anything else out of scope?
```

Answering "the person cutting the release; a generated changelog draft; must not write CHANGELOG.md directly" turns the fuzzy idea into exactly the request used above, and step 1 restates and classifies it from there. In a non-interactive run, or if the user has nothing to add, plan-writer proceeds on its own best reading and records the assumption under "Decisions made" instead of stopping.
