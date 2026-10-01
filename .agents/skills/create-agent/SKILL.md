---
name: create-agent
description: Writes a persona folder under agents/ for one existing skill, with a Claude Code file, an OpenCode file, and a Cursor file. Sets the description, the mode (agent, subagent, or all), a system prompt that loads the skill, and the tool permissions each product can enforce. Use when creating an agent, adding a persona, or wrapping a skill for Claude Code, Cursor, or OpenCode.
license: CC-BY-ND-4.0
metadata:
  version: "0.1.0"
  category: workflow
---

# create-agent

## Purpose

create-agent writes the persona that runs a skill. The skill stays the procedure. The persona is the role a person switches to or invokes: `tech-writer` for the `tech-writing` skill, `plan-reviewer` for `plan-review`. It writes three tool files and a one-line sidecar. It does not copy the skill body into the prompt, and it does not edit `SKILL.md`.

## When to use / when NOT to use

Use create-agent when you:

- Add a persona for a skill that already exists under `skills/`.
- Set `metadata.suggested-model` on a skill and need the matching `agents/<persona>/` folder.
- Change a persona's mode, system prompt, or tool permissions.

Do NOT use create-agent when you:

- Author or review the skill's own `SKILL.md`. That is `skill-writer`.
- Install a persona into a tool. That is `scripts/agent-install.sh`.
- Edit the validator, the installer, or repository docs.

## Workflow

1. Take the persona name. It is lowercase words separated by hyphens, 1 to 64 characters, and a role (`tech-writer`). It may equal the skill name when that name is already a role (`plan-writer`). Refuse a name that is already a folder under `agents/`.
2. Take the skill it loads. The directory `skills/<category>/<skill>/` must already exist. Read that `SKILL.md` once. If it does not exist, stop and say so.
3. Take the mode: `agent`, `subagent`, or `all`. `agent` is written as OpenCode `mode: primary`. `subagent` and `all` are written unchanged. Write `mode` only in `opencode.md`. In the Claude Code and Cursor bodies, say that those tools invoke a subagent.
4. Write a system prompt that names the persona, says whether this session is the OpenCode primary session or a Claude Code or Cursor subagent, and tells the agent to load the skill and follow its Workflow. When the mode is `agent` or `all`, say this is an interactive session and follow the steps the skill marks that way. When the mode is `subagent` and the skill describes a non-interactive run, follow that run. Do not paste the skill body. Do not invent a section name the skill does not have.
5. Allow only the tools that workflow uses. OpenCode gets a `permissions` list: a broad deny first, then exceptions, with `skill` denied on `*` and then allowed for the named skill plus any skill the workflow loads by name. Scope `edit` to a directory only when the workflow writes to that directory and nowhere else. In that list, `*` already matches `/`, so a directory limit looks like `docs/plans/*`. A second asterisk is a literal `*` and would block the write. Use the directory that workflow writes to, not `docs/plans/`, unless that is the directory. Claude Code gets a `tools` allowlist and no `permissionMode`. Cursor gets `readonly: false` when the workflow writes a file, and `readonly: true` when it does not.
6. When the skill sets `metadata.suggested-model`, pin `model` from it. Claude Code uses `fable` for `anthropic/claude-fable-5-1` and `opus` for any other model, and says so in the file, because Claude Code runs Claude models only. OpenCode uses the `provider/model` id. Cursor uses the model id without the provider, plus `[effort=<level>]` when `metadata.suggested-effort` is set. When the skill has no hint, omit `model` and say in the file that no model is pinned.
7. Write `agents/<persona>/skill` as one line, the skill directory name. Write `claude-code.md`, `opencode.md`, and `cursor.md`. All three or none. Claude Code and Cursor `name` equal the persona folder. OpenCode has no `name` field.

### Handling feedback

A hedged remark about a permission or a mode — "I'm not sure this should be a subagent" — never changes the folder by itself. Restate the workflow step that needs the tool or the mode, give a recommendation, and ask a yes/no question. A plain instruction — "make it primary" — is applied directly.

## Output format

```text
agents/<persona>/skill            one line: the skill directory name
agents/<persona>/claude-code.md
agents/<persona>/opencode.md
agents/<persona>/cursor.md
```

Claude Code and Cursor frontmatter include `name` (the persona), `description`, and `model` when a hint exists. Claude Code also includes `tools` and `skills: [<skill>]`. Cursor also includes `readonly`. OpenCode frontmatter includes `description`, `mode` (`primary`, `subagent`, or `all`), `model` when a hint exists, and a `permissions` list of `action`, `resource`, and `effect`. The body of each file names the skill as its own word and does not paste the skill.

## Guardrails

MUST:

- MUST set `name` to `create-agent` and `metadata.version` to a quoted SemVer string.
- MUST keep `license` at `CC-BY-ND-4.0`.
- MUST write the persona under `agents/<persona>/`, never under `skills/<category>/<skill>/agents/`.
- MUST load the skill from the prompt or from Claude Code's `skills` field. Never paste the skill body.
- MUST write all three tool files, or none.
- MUST keep Claude Code and Cursor `name` equal to the persona folder.
- MUST write OpenCode `mode` as `primary` when the requested mode is `agent`.
- MUST keep the description in third person and under 700 characters.

NEVER:

- NEVER set `metadata.suggested-model` on this skill. It does not ship a persona of its own.
- NEVER set Claude Code `permissionMode`.
- NEVER put `model`, `effort`, or `context` in a `SKILL.md`.
- NEVER invent a tool name. Claude Code tool names are `Read`, `Grep`, `Glob`, `Edit`, `Write`, `WebSearch`, `WebFetch`, `Bash`, and `Skill`. An unknown name stops the agent from launching.
- NEVER commit a secret, personal data, an internal URL, or an absolute local path.

## QA checklist

- [ ] `agents/<persona>/skill` is one line and names a skill directory that exists.
- [ ] `claude-code.md`, `opencode.md`, and `cursor.md` all exist.
- [ ] Claude Code and Cursor `name` equal the persona folder.
- [ ] OpenCode `mode` is `primary`, `subagent`, or `all`.
- [ ] Each body names the skill as its own word and does not paste the skill.
- [ ] OpenCode `permissions` use `allow`, `ask`, or `deny`, with broad rules before exceptions.
- [ ] Claude Code has a `tools` allowlist and no `permissionMode`.
- [ ] Cursor `readonly` matches whether the workflow writes a file.
- [ ] `bun run validate` reports 0 errors.

<!-- markdownlint-disable-next-line MD034 -->
© Sillybit — https://github.com/Sillybit-io/silly-skills — CC BY-ND 4.0. Attribution required; do not republish modified versions without written approval.
