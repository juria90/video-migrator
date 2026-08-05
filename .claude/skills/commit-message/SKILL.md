---
name: commit-message
description: Generate a short git commit message for the staged changes, or the working-tree changes if nothing is staged.
---

Generate a short git commit message. Prefer the **staged** changes; if nothing is staged, fall back to the unstaged **working-tree** changes.

1. Inspect the changes. Run each command as its **own plain Bash call** — do NOT chain with `&&`/`||`/`;` or wrap in `{ … }` / `( … )`. The permission allowlist matches simple prefixes (`Bash(git diff *)`); grouped/compound one-liners defeat that match and trigger a confirmation prompt.
   - `git diff --cached --stat` — staged file overview. If it lists files, describe the **staged** changes (run `git diff --cached` for the content) and skip the fallback below.
   - If `git diff --cached --stat` printed nothing, nothing is staged → describe the unstaged **working-tree** changes instead: `git diff --stat` for the overview, then `git diff` for the content. (`git diff` omits untracked files; if `git diff --stat` is also empty, run `git status --short` — any remaining changes are untracked-only, so note them by path since there is nothing tracked to diff.)
   - If staged, unstaged, and untracked are all empty, there is nothing to commit — say so and stop.
   Read enough to know *what changed and why*, not just which files.

2. Write the message in this repo's Conventional-Commits style (see the git log, e.g. `feat(s09-7): …`, `docs(s06-2): …`):
   - Subject line: `type(scope): summary` — imperative mood, lower-case, **no trailing period**, aim for ≤ 50 chars (hard cap 72).
   - `type` ∈ `feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `build` / `ci` / `perf`.
   - `scope` is the UC1 slot when the change maps to one (`s06-5`), else the package/area (`vp-api`, `frontend`, `api`).
   - Keep it to the subject line unless the diff has more than one distinct logical change; only then add a blank line and 1–3 terse `-` bullets.

3. Output the message in a fenced block so it is easy to copy. Do **not** run `git commit` — this skill only drafts the message. If the message describes **working-tree** (unstaged) changes, note they must be `git add`-ed before committing. (If the user later asks to commit, do NOT append the `Co-Authored-By` trailer per `.claude/CLAUDE.md`.)
