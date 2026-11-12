---
name: cross-review
description: Have other coding agents (Codex by default, or any reviewer CLI defined in the config, several at once) independently review your code change before the user sees it, then fix what is valid and report the outcome. Use after you finish implementing a change and before you show it to the user or ask to commit; also use after opening a pull request, to read and answer the Codex review on it. Arguments - optional reviewer names ("codex,gemini") or "pr <number>".
user-invocable: true
---

# cross-review

Other agents review your work before the user does. You stay responsible for the change: a reviewer's finding is input to judge, not an order.

`SCRIPT` below means `python3 "${CLAUDE_PLUGIN_ROOT}/skills/cross-review/scripts/cross_review.py"`.

## When to run

- **Before showing work** (mode 1): every time you finish implementing a code change, after your own tests and build pass, and before you show the change to the user or ask to commit. Skip it only for changes with no code (for example a typo in prose) and say you skipped it.
- **After opening a pull request** (mode 2): the PR reviewer (Codex's GitHub integration by default) reviews it on GitHub. Read that review and answer it.
- Whenever the user asks: `/cross-review`, `/cross-review gemini`, `/cross-review codex,gemini`, `/cross-review pr 12`.

## Configuration

Settings merge in this order, later wins: the plugin's `defaults.json` → user config `~/.config/cross-review/config.json` → repository `.cross-review.json` → arguments.

- `reviewers`: names to run, in parallel. Each name must have an entry in `definitions`.
- `definitions.<name>.command`: shell command run in the repository root. The review prompt arrives on stdin. Placeholders `{repo}`, `{prompt}` (prompt file) and `{output}` (where to write the result) are shell-quoted for you. If the command does not write `{output}`, its stdout is the result. Optional `check` is run first (for example `codex login status`); if it fails, that reviewer is skipped with `status: error`.
- `enabled: false` turns the plugin off for a repository (for example a private project whose code should not go to other providers).
- `base`, `maxRounds`, `timeoutSeconds`, `maxDiffBytes`, `exclude`, `language`, and `pr` (`reviewer`, `request: "auto" | "comment"`, `requestComment`, `botLogins`).

`SCRIPT config` prints the resolved settings.

## Mode 1: before showing work

1. Write the task goal in two or three lines: the issue number, title and gist if there is one, otherwise what the user asked for. Describe the goal only. Do not include your conversation with the user, your design reasons, or your own judgment of the change, so the reviewers form their own view.
2. Run:
   ```
   SCRIPT run --goal "<goal>" --checks "<checks you ran and their result>" [--reviewers codex,gemini] [--base <ref>]
   ```
   Run it in the background if your tool allows long commands; a review can take several minutes. The script prints JSON with one result per reviewer: `status`, `output` (the review file), and `log`.
3. Read each `output` file. If a reviewer failed (`status: error`), read its `log`, tell the user which reviewer could not run and why (not installed, not signed in, timed out), and continue with the others.
4. Judge every finding against the code and the project's rules:
   - Valid: fix it.
   - Not valid or out of scope: keep a one-line reason.
   - Several reviewers raising the same point count as one finding.
5. After fixing, rerun your tests and build. If you changed code because of findings, run the review again, up to `maxRounds` runs in total (default 2). Do not loop past that; list what is still open instead.
6. In your report to the user, add a short section in the user's language:
   - which reviewers ran (and any that failed),
   - findings you fixed,
   - findings you did not fix, each with the reason,
   - anything still open after the last round.

## Mode 2: after opening a pull request

1. If `pr.request` is `"comment"` (default), ask for the review right after `gh pr create`: `SCRIPT pr-request <number>`. With `"auto"` the reviewer's GitHub integration reviews every new pull request on its own, so do nothing.
2. The review arrives minutes later. Do not wait in a loop. On your next turn, or when the user asks, run `SCRIPT pr-feedback <number>`. It prints the bot's reviews and inline comments, each with the commit it was written for and the command to reply to it.
3. If there is no review yet, say so and check again on a later turn.
4. Judge each comment as in mode 1. Fix valid ones on the PR branch, run tests and build, and push only if the user's rules for this project allow pushing without asking; otherwise show the fix and ask first.
5. Reply to each inline comment (`gh api repos/<owner>/<repo>/pulls/<n>/comments/<id>/replies -f body=...`), starting the reply with `🤖 Claude:` and then "Fixed in <short sha>" or "Not changed: <reason>". These replies are posted with the user's GitHub account, so keep them factual and short.
6. Tell the user in one short summary what the review raised and what you did.

## Notes

- Reviewers run read-only in the repository (Codex: `--sandbox read-only`). Files matching `exclude` (secrets, lock files) are left out of the diff and reviewers are told not to open secret files.
- The change and the files reviewers read go to each reviewer's provider. Respect `enabled: false`.
