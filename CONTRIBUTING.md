# Contributing

VeriFact is built by a two-person team working with AI coding agents. This page explains the loop every change goes through. The rules themselves live in [`AGENTS.md`](AGENTS.md) and [`.agents/rules/`](.agents/rules/); this page links to them rather than repeating them.

## The loop
1. **Decide.** A decision is recorded in [`project_context/DECISIONS.md`](project_context/DECISIONS.md), newest first. A replaced decision is marked SUPERSEDED, never deleted.
2. **Spec.** Each task is one file, `steering_docs/specs/NN-name.md`, with six sections: Goal · Inputs/outputs (exact names) · Steps · Numbered acceptance criteria · Out of scope · Open questions. Keep a spec small enough for one agent task.
3. **Build.** Antigravity runs `/implement-spec steering_docs/specs/NN-name.md` ([workflow](.agents/workflows/implement-spec.md)). It stops at open questions and reports each acceptance criterion with evidence.
4. **Review.** Claude Code reviews the change against the spec and the rule IDs (P* principles, S* secrets and cost). It reports findings by rule ID or criterion number.
5. **Merge.** A human merges once every criterion is met, or the gap is written down as a new open question.

## Branches and commits
- One branch per spec: `spec/NN-name`. One PR per spec, titled `spec NN: <goal>`.
- `main` always opens on a working replay of the first event.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <summary>

<optional body: why, not what>

Spec: NN
```

| Type | Use for |
| --- | --- |
| `feat` | New behaviour a user or another service can see |
| `fix` | A bug fix |
| `test` | Tests or fixtures only |
| `docs` | READMEs, steering docs, comments |
| `refactor` | Code change with no behaviour change |
| `chore` | Dependencies, config, `.gitignore`, tooling |
| `ci` / `build` | Deploy scripts, Dockerfiles, Cloud Build |

- **Scope** is the service or area: `x-backfill`, `collect-hn`, `enrich`, `api`, `check`, `web`, `extension`, `core`, `infra`, `events`, `rules`.
- **Summary**: imperative, lower case, no full stop, at most 72 characters. For example, `feat(enrich): store stance agreement across 3 prompts`.
- **`Spec: NN`** trailer on every commit made for a spec. Omit it only for work outside a spec (for example `chore(rules): ...`).
- Add a `BREAKING CHANGE:` footer when a table, field or endpoint name changes.
- Never put secrets, keys or user data in a commit message.

## Before you push
- The checklists in [`principles.md`](.agents/rules/principles.md), [`stack-and-conventions.md`](.agents/rules/stack-and-conventions.md) (event-literal check) and [`secrets-and-cost.md`](.agents/rules/secrets-and-cost.md) (secret grep) all pass.
- No secrets, `.env` files or service-account keys. No live X API calls in tests.
- `_legacy/` is unchanged.

## Changing the rules
Change a rule file only when a matching DECISIONS.md entry exists. Keep each file under 12,000 characters (the Antigravity limit).

## Adding an event
Add a config file, not code: see [`project_context/SEEDS.md`](project_context/SEEDS.md), Part 3.
