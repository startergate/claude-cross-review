#!/usr/bin/env python3
"""cross-review: run reviewer CLIs on the current change and collect their findings.

Subcommands
  run          Build the review prompt and run the selected reviewers in parallel.
  config       Print the resolved configuration for a repository.
  pr-feedback  Print a Codex (or other bot) review on a pull request.
  pr-request   Ask the PR reviewer for a review by posting its request comment.
  hook-session-start, hook-pr-created  Used by the plugin's hooks.

Only the Python 3.9 standard library is used.
"""

import argparse
import concurrent.futures
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS_PATH = os.path.join(SKILL_DIR, "defaults.json")
PROMPT_PATH = os.path.join(SKILL_DIR, "prompt.md")
REPO_CONFIG_NAME = ".cross-review.json"


# ---------------------------------------------------------------- helpers

def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), result.stderr.strip()))
    return result.stdout


def repo_root(path):
    try:
        return git(path, "rev-parse", "--show-toplevel").strip()
    except RuntimeError:
        return None


def user_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "cross-review", "config.json")


def read_json(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def merge(base, override):
    """Merge override into base. definitions and pr merge per key; everything else replaces."""
    out = dict(base)
    for key, value in override.items():
        if key in ("definitions", "pr") and isinstance(value, dict):
            merged = dict(out.get(key) or {})
            for name, item in value.items():
                if key == "definitions" and isinstance(item, dict) and isinstance(merged.get(name), dict):
                    merged[name] = dict(merged[name], **item)
                else:
                    merged[name] = item
            out[key] = merged
        else:
            out[key] = value
    return out


def load_config(repo):
    config = read_json(DEFAULTS_PATH)
    sources = [DEFAULTS_PATH]
    for path in (user_config_path(), os.path.join(repo, REPO_CONFIG_NAME) if repo else None):
        data = read_json(path)
        if data:
            config = merge(config, data)
            sources.append(path)
    config["_sources"] = sources
    return config


def split_names(value):
    if isinstance(value, str):
        value = value.split(",")
    return [v.strip() for v in value or [] if v and v.strip()]


def is_excluded(path, patterns):
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


def default_base(repo):
    candidates = []
    head = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False).strip()
    for ref in ("origin/develop", "develop"):
        candidates.append(ref)
    if head:
        candidates.append(head.replace("refs/remotes/", ""))
    candidates += ["origin/main", "main", "origin/master", "master"]
    for ref in candidates:
        if git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).strip():
            return ref
    return None


# ---------------------------------------------------------------- diff

def build_diff(repo, base, patterns, max_bytes):
    merge_base = git(repo, "merge-base", base, "HEAD").strip() if base else None
    target = merge_base or "HEAD"
    changed = [p for p in git(repo, "diff", "--name-only", target).splitlines() if p]
    untracked = [p for p in git(repo, "ls-files", "--others", "--exclude-standard").splitlines() if p]

    kept = [p for p in changed if not is_excluded(p, patterns)]
    skipped = [p for p in changed + untracked if is_excluded(p, patterns)]

    parts = []
    if kept:
        parts.append(git(repo, "diff", "--no-color", target, "--", *kept))
    for path in untracked:
        if is_excluded(path, patterns):
            continue
        result = subprocess.run(
            ["git", "-C", repo, "diff", "--no-color", "--no-index", "--", "/dev/null", path],
            capture_output=True,
            text=True,
            errors="replace",
        )
        parts.append(result.stdout)
    diff = "".join(parts)

    truncated = ""
    if len(diff.encode("utf-8")) > max_bytes:
        diff = diff.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        truncated = (
            " The diff was cut at %d bytes; read the remaining changed files in the repository "
            "(`git diff %s` lists them)." % (max_bytes, target)
        )
    if skipped:
        truncated += " Left out of the diff on purpose (secrets or lock files): %s." % ", ".join(sorted(set(skipped)))
    return {
        "diff": diff,
        "merge_base": merge_base or "(none: compared with HEAD)",
        "files": sorted(set(kept + [p for p in untracked if not is_excluded(p, patterns)])),
        "note": truncated,
    }


def render_prompt(config, goal, checks, base, branch, diff_info):
    with open(PROMPT_PATH, encoding="utf-8") as f:
        text = f.read()
    values = {
        "{exclude}": ", ".join("`%s`" % p for p in config.get("exclude", [])),
        "{language}": config.get("language") or "English",
        "{goal}": goal.strip() or "(not given)",
        "{checks}": checks.strip() or "(none reported)",
        "{base}": base or "(none)",
        "{merge_base}": diff_info["merge_base"],
        "{branch}": branch,
        "{truncated}": diff_info["note"],
    }
    for key, value in values.items():
        text = text.replace(key, value)
    # The diff goes in last so text inside it is never treated as a placeholder.
    return text.replace("{diff}", diff_info["diff"] or "(no changes)")


# ---------------------------------------------------------------- run

def run_reviewer(name, definition, repo, prompt_path, out_dir, timeout):
    output = os.path.join(out_dir, name + ".md")
    log = os.path.join(out_dir, name + ".log")
    command = definition.get("command")
    if not command:
        return {"reviewer": name, "status": "error", "error": "no command defined"}
    command = (
        command.replace("{repo}", shlex.quote(repo))
        .replace("{prompt}", shlex.quote(prompt_path))
        .replace("{output}", shlex.quote(output))
    )
    started = time.time()
    check = definition.get("check")
    if check:
        probe = subprocess.run(check, shell=True, cwd=repo, capture_output=True, text=True, timeout=60)
        if probe.returncode != 0:
            detail = (probe.stdout + probe.stderr).strip().splitlines()
            return {
                "reviewer": name,
                "status": "error",
                "error": "not ready (`%s` failed: %s)" % (check, detail[-1] if detail else "no output"),
                "seconds": round(time.time() - started),
            }
    try:
        with open(prompt_path, "rb") as stdin, open(log, "wb") as stdout:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=repo,
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        status = "ok" if proc.returncode == 0 else "error"
        error = None if status == "ok" else "exit code %d (see log)" % proc.returncode
    except subprocess.TimeoutExpired:
        status, error = "error", "timed out after %ds" % timeout
    # A reviewer that only prints to stdout gets its stdout as the result.
    if not (os.path.isfile(output) and os.path.getsize(output) > 0) and os.path.isfile(log):
        with open(log, "rb") as src, open(output, "wb") as dst:
            dst.write(src.read())
    return {
        "reviewer": name,
        "status": status,
        "error": error,
        "seconds": round(time.time() - started),
        "output": output,
        "log": log,
    }


def cmd_run(args):
    repo = repo_root(args.repo or os.getcwd())
    if not repo:
        sys.exit("cross-review: not inside a git repository")
    config = load_config(repo)
    if not config.get("enabled", True) and not args.force:
        print(json.dumps({"skipped": "cross-review is disabled for this repository (enabled: false)"}))
        return

    names = split_names(args.reviewers) or split_names(config.get("reviewers")) or ["codex"]
    definitions = config.get("definitions") or {}
    unknown = [n for n in names if n not in definitions]
    if unknown:
        sys.exit("cross-review: no definition for reviewer(s): %s" % ", ".join(unknown))

    base = args.base or config.get("base") or default_base(repo)
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip() or "(detached)"
    goal = args.goal or ""
    if args.goal_file:
        with open(args.goal_file, encoding="utf-8") as f:
            goal = f.read()

    diff_info = build_diff(repo, base, config.get("exclude") or [], int(config.get("maxDiffBytes") or 200000))
    if not diff_info["diff"].strip():
        print(json.dumps({"skipped": "no changes against %s" % (base or "HEAD")}))
        return

    out_dir = tempfile.mkdtemp(prefix="cross-review-")
    prompt_path = os.path.join(out_dir, "prompt.md")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(render_prompt(config, goal, args.checks or "", base, branch, diff_info))

    timeout = int(config.get("timeoutSeconds") or 900)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = [
            pool.submit(run_reviewer, n, definitions[n], repo, prompt_path, out_dir, timeout)
            for n in names
        ]
        results = [f.result() for f in futures]

    print(json.dumps({
        "repo": repo,
        "base": base,
        "branch": branch,
        "files": diff_info["files"],
        "prompt": prompt_path,
        "maxRounds": config.get("maxRounds", 2),
        "results": results,
    }, indent=2, ensure_ascii=False))


def cmd_config(args):
    repo = repo_root(args.repo or os.getcwd())
    config = load_config(repo)
    config["_userConfig"] = user_config_path()
    config["_repoConfig"] = os.path.join(repo, REPO_CONFIG_NAME) if repo else None
    config["_resolvedBase"] = (config.get("base") or default_base(repo)) if repo else None
    print(json.dumps(config, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- pull requests

def gh_json(*args):
    result = subprocess.run(["gh"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("gh %s failed: %s" % (" ".join(args), result.stderr.strip()))
    return json.loads(result.stdout or "null")


def gh_paginated(path):
    items = gh_json("api", "--paginate", "--slurp", path)
    flat = []
    for page in items or []:
        flat.extend(page if isinstance(page, list) else [page])
    return flat


def cmd_pr_feedback(args):
    repo = repo_root(args.repo or os.getcwd())
    config = load_config(repo)
    bots = set(config.get("pr", {}).get("botLogins") or [])
    os.chdir(repo or os.getcwd())
    pr = gh_json("pr", "view", str(args.pr), "--json", "number,url,headRefOid,headRefName,baseRefName")
    nwo = "/".join(pr["url"].split("/")[3:5])  # owner/repo of the PR itself
    number = pr["number"]

    def by_bot(item):
        login = (item.get("user") or {}).get("login", "")
        return login in bots or login.removesuffix("[bot]") in bots

    reviews = [r for r in gh_paginated("repos/%s/pulls/%d/reviews" % (nwo, number)) if by_bot(r)]
    comments = [c for c in gh_paginated("repos/%s/pulls/%d/comments" % (nwo, number)) if by_bot(c)]
    issue_comments = [c for c in gh_paginated("repos/%s/issues/%d/comments" % (nwo, number)) if by_bot(c)]

    lines = ["# Bot review on %s" % pr["url"], "", "Head commit: %s" % pr["headRefOid"], ""]
    if not (reviews or comments or issue_comments):
        lines.append("No review from %s yet." % ", ".join(sorted(bots)))
    for r in reviews:
        lines += ["## Review %s (%s, commit %s)" % (r["id"], r.get("state"), (r.get("commit_id") or "")[:7]), "", r.get("body") or "(no body)", ""]
    for c in comments:
        where = "%s:%s" % (c.get("path"), c.get("line") or c.get("original_line"))
        lines += [
            "## Inline comment %s at %s (commit %s)" % (c["id"], where, (c.get("commit_id") or "")[:7]),
            "Reply with: gh api repos/%s/pulls/%d/comments/%s/replies -f body=..." % (nwo, number, c["id"]),
            "",
            c.get("body") or "",
            "",
        ]
    for c in issue_comments:
        lines += ["## Comment %s" % c["id"], "", c.get("body") or "", ""]
    print("\n".join(lines))


def cmd_pr_request(args):
    repo = repo_root(args.repo or os.getcwd())
    config = load_config(repo)
    body = config.get("pr", {}).get("requestComment") or "@codex review"
    os.chdir(repo or os.getcwd())
    subprocess.run(["gh", "pr", "comment", str(args.pr), "--body", body], check=True)


# ---------------------------------------------------------------- hooks

def hook_output(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def cmd_hook_session_start(_args):
    repo = repo_root(os.getcwd())
    config = load_config(repo)
    if not config.get("enabled", True):
        return
    names = ", ".join(split_names(config.get("reviewers")) or ["codex"])
    hook_output(
        "SessionStart",
        "cross-review plugin is active (reviewers: %s). When you finish implementing a code change, "
        "and before you show it to the user or ask to commit it, run the cross-review skill on it, "
        "address the findings, and include the review outcome in your report. "
        "After you open a pull request, follow the skill's pull request steps." % names,
    )


# github.com or a GitHub Enterprise host, as printed by gh pr create
PR_URL = re.compile(r"https://[\w.-]+(?::\d+)?/[\w.-]+/[\w.-]+/pull/\d+")


def tool_text(value):
    """Flatten a hook's tool_response (dict, list or string) into one string."""
    if isinstance(value, dict):
        return "\n".join(tool_text(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(tool_text(v) for v in value)
    return value if isinstance(value, str) else ""


def cmd_hook_pr_created(_args):
    try:
        payload = json.load(sys.stdin) or {}
    except ValueError:
        payload = {}
    command = ((payload.get("tool_input") or {}).get("command")) or ""
    if "gh pr create" not in command:
        return
    urls = PR_URL.findall(tool_text(payload.get("tool_response")))
    if not urls:
        return  # the PR was not created (dry run, error, or --web)
    url = urls[-1]
    repo = repo_root(payload.get("cwd") or os.getcwd())
    config = load_config(repo)
    if not config.get("enabled", True):
        return
    pr = config.get("pr") or {}
    if pr.get("request") == "comment":
        body = pr.get("requestComment") or "@codex review"
        result = subprocess.run(
            ["gh", "pr", "comment", url, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            step = "The review request (`%s`) was posted on %s automatically; do not post it again. " % (body, url)
        else:
            step = (
                "Posting the review request on %s failed (%s). Post it with "
                "`cross_review.py pr-request %s` and tell the user if that fails too. "
                % (url, (result.stderr or result.stdout).strip()[:300], url)
            )
    else:
        step = "The %s GitHub integration reviews new pull requests on its own. " % pr.get("reviewer", "codex")
    hook_output(
        "PostToolUse",
        "cross-review: a pull request was just opened. " + step +
        "The review arrives later: check it with `cross_review.py pr-feedback %s` on your next turn "
        "or when the user asks, then fix what is valid, push, and reply to each review comment." % url,
    )


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(prog="cross_review.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run")
    p.add_argument("--repo")
    p.add_argument("--reviewers", help="comma-separated reviewer names; overrides the config")
    p.add_argument("--base", help="base branch or ref; default: config, then develop, then the default branch")
    p.add_argument("--goal", help="what the change is for (issue title and body, or a short summary)")
    p.add_argument("--goal-file")
    p.add_argument("--checks", help="checks already run and their result, e.g. 'bun test: pass'")
    p.add_argument("--force", action="store_true", help="run even if enabled is false")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("config")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("pr-feedback")
    p.add_argument("pr")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_pr_feedback)

    p = sub.add_parser("pr-request")
    p.add_argument("pr")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_pr_request)

    sub.add_parser("hook-session-start").set_defaults(func=cmd_hook_session_start)
    sub.add_parser("hook-pr-created").set_defaults(func=cmd_hook_pr_created)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
