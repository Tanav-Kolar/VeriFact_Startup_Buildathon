---
description: Build one spec from steering_docs/specs/ end to end, then report each acceptance criterion as met or not met, with evidence.
---

# Implement a spec

Usage: `/implement-spec steering_docs/specs/NN-name.md`

1. **Read.** Read the spec in full, then `AGENTS.md`, the rules in `.agents/rules/`, the newest entries in `project_context/DECISIONS.md`, and every doc the spec links. If two sources disagree, use the precedence in `AGENTS.md`.
2. **Check for blockers.** If the spec has unresolved "Open questions" that affect this build, or a step needs a decision the spec does not make, stop. List the questions and wait for answers.
3. **Plan.** Write a short plan: the files you will create or change, and which acceptance criterion each step satisfies. List any rule ID (P*, S*) that the work touches. Wait for approval if the plan touches cloud resources, paid APIs or anything outside the spec's files.
4. **Build.** Implement only what the spec says, using its exact names. No work from "Out of scope". Add tests for each acceptance criterion that can be tested in code; mock external APIs.
5. **Verify.** Run the tests, `ruff check`, the review checklist in `principles.md`, the event-literal check in `stack-and-conventions.md`, and the pre-commit secret grep in `secrets-and-cost.md`. Fix any failures.
6. **Report.** Reply with a table: each numbered acceptance criterion, met or not met, and the evidence (test name, command and output, or file and line). Then list: files changed, anything you did not do and why, and new open questions. Never mark a criterion met without evidence.
7. **Commit.** Use Conventional Commits, following the commit rules in `CONTRIBUTING.md`: `<type>(<scope>): <summary>` with a `Spec: NN` trailer. Small, focused commits are fine; each must leave tests passing. Do not push unless asked.
