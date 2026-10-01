# External research

Read this file after the codebase exploration and, when the request came from a ticket, after the vagueness test. Skip it only when the run already stopped as `trivial` or as a blocked ticket.

## First pass

Search the public web for this feature, not for generic best practice. The queries come from what the user asked: the official docs, blog posts, how other people implemented it, what they treat as important, and the pros and cons.

Budget: 5 searches and 5 pages read, whichever comes first. At the ceiling, write what you have and list the queries you did not run. The run still continues.

Write `## Research` in the plan:

- Sources. Each source is an `http` or `https` URL you opened.
- How others did it.
- What matters.
- Pros and cons.
- What this plan will follow.

When the public web has nothing useful, write one line instead of inventing sources: `No useful public source found.` Name the queries you ran. Then continue.

A research finding does not replace a code citation. Every IS cell still cites `path:line`. Put a research finding in the GAP cell or in this section.

## Second pass

Run this only after the owner has answered the product and technical questions, and only when an answer introduces a product choice, a technology, a constraint, or a non-goal that `## Research` does not already cover. Confirming a recorded default is not a new point.

Budget: 3 pages on that new point. At the ceiling, write what you have. Append the result under `### After answers`. Update the IS/GAP ledger and the Risks table when the pass changes them.

Do not open another question round. The one exception: the pass creates an irreversible, destructive, costly, or cross-cutting fork that nobody has answered. Write that one question, with its reason and its default, into `## Questions`, and stop. There is no third pass.

## Fix-only

When `plan-review` names a missing or broken `## Research` section, this same budget applies. Do not re-explore the codebase to fill it.
