# CareerLens Development Rules

## Canonical workspace

- The canonical active CareerLens repository is `/home/sagar/projects/CareerLens`.
- All CareerLens development, editing, testing, Git operations, Docker/Compose operations, documentation work, and tooling must be performed from this WSL/Fedora repository.
- `/mnt/d/Project/CareerLens` is a backup/archive copy only. Do not edit, commit, test, or run CareerLens from that copy unless explicitly instructed by Sagar.
- Do not create or maintain a second active working copy of CareerLens.

## WSL-only development

- Use the native Fedora/WSL toolchain for CareerLens.
- Do not switch CareerLens development to Windows-installed Python, Node.js, npm, Git, Docker, or related tooling when an equivalent WSL tool is available.
- Keep application dependencies and development tooling inside WSL.

## Git identity and attribution — STRICT

- The sole project author identity is Sagar Saitwal <sagar.saitwal@outlook.com>.
- Never add, preserve, or generate any AI-assistant attribution, contributor attribution, co-author trailer, or equivalent authorship metadata in Git commits, Git history, documentation, source files, or repository metadata.
- Never modify Git configuration to introduce an assistant as an author, committer, or co-author.
- Never add an assistant identity to commit messages or trailers.
- Before committing, verify the commit author and committer are Sagar Saitwal <sagar.saitwal@outlook.com>.
- If an existing change contains assistant-attribution metadata, remove it before committing rather than carrying it forward.
- Do not create commits that contain authorship-attribution trailers for an AI assistant.

## Repository safety

- Do not delete, reset, rewrite, force-push, or otherwise destroy Git history or project files unless Sagar explicitly requests it.
- Do not delete or overwrite the backup/archive copy unless explicitly instructed.
- Do not modify `.env` or expose its contents in commits, logs, documentation, prompts, or responses.
- Respect `.gitignore` and keep secrets out of Git.

## Architecture and design rules

- Before making architectural decisions, inspect the relevant CareerLens documentation, especially:
  - `docs/REQUIREMENTS.md`
  - `docs/ARCHITECTURE.md`
  - `docs/DATABASE.md`
  - `docs/AI-MATCHING.md`
  - `docs/ROADMAP.md`
  - `docs/SYSTEM-REQUIREMENTS.md`
  - `README.md`
- Preserve approved architecture and invariants unless Sagar explicitly approves a change.
- Do not silently invent deferred design decisions.
- Keep deterministic matching logic deterministic; AI/LLM components must not take ownership of deterministic scoring, category assignment, catalog mutation, or gap creation.
- Preserve the approved separation between fit evidence and preference evidence.
- Preserve coverage-aware fit scoring and the distinction between confirmed gaps and indeterminate comparisons.
- Preserve immutable `ProfileVersion` semantics for matching inputs and auditability.
- Preserve catalog-version and rule/model-version auditability for matching results.

## Implementation discipline

- Make the smallest coherent change that satisfies the requested task.
- Inspect existing code and tests before changing behavior.
- Add or update tests for behavior changes.
- Run the relevant tests, linting, type checks, and validation before reporting completion.
- Report what changed, what was tested, and any known limitations.
- Do not claim a task is complete when validation has not been run or has failed.

## Git workflow

- Work from `/home/sagar/projects/CareerLens`.
- Keep commits focused and meaningful.
- Use Sagar's configured Git identity for commits.
- Do not push with force unless Sagar explicitly requests it.
- Do not change the GitHub remote or repository configuration unless explicitly instructed.

## Claude Code operating rule

- Claude Code may edit files only in the canonical WSL workspace unless Sagar explicitly authorizes another path.
- Do not edit `/mnt/d/Project/CareerLens` during normal development.
- If a task appears to require changes outside the canonical workspace, stop and ask Sagar before making those changes.
