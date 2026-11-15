#!/usr/bin/env python3
"""PreToolUse hook: block Claude from merging a PR until CodeRabbit has passed.

Runs before every Bash command. When the command merges a pull request
(`gh pr merge`, or the merge endpoints through `gh api`), the call is denied
unless, on the PR's current head commit:

  - CodeRabbit's `CodeRabbit` check run completed with conclusion `success`
  - CodeRabbit's latest review is not CHANGES_REQUESTED
  - every review thread CodeRabbit opened is resolved

Anything that stops the check from running denies the merge (fail closed).

This guards Claude only. GitHub's "Protect main" ruleset requires resolved
review threads and the CI checks, but not CodeRabbit's check: CodeRabbit
skips some PRs, and a required check that never passes blocks the merge.
"""

import json
import os
import re
import shlex
import subprocess
import sys

CODERABBIT_APP = "coderabbitai"
CHECK_NAME = "CodeRabbit"

# `gh pr merge` flags that take a value, so the value isn't read as the PR argument.
VALUE_FLAGS = {
    "-t", "--subject", "-b", "--body", "-F", "--body-file",
    "--match-head-commit", "-A", "--author-email",
}
SEPARATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "(", ")"}
SHELLS = {"bash", "sh", "zsh"}

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      latestReviews(first: 100) { nodes { state author { login } } }
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved comments(first: 1) { nodes { author { login } url } } }
      }
    }
  }
}
"""


class CheckError(Exception):
    pass


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def is_redirect(tok):
    return bool(tok) and tok[0] in "<>&" and set(tok) <= set("<>&|") and ("<" in tok or ">" in tok)


def tokenize(command):
    # Newlines separate commands; inside quotes the swap only changes text we never read.
    command = command.replace("\n", " ; ")
    # Drop fd numbers glued to a redirect (the `2` in `2>&1`); shlex would split them off as words.
    command = re.sub(r"(?<![\w-])\d+(?=[<>])", "", command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def find_merges(command):
    """Return (selector, repo) for each `gh pr merge` in command. selector None = current branch."""
    tokens = tokenize(command)
    merges = []
    for i, tok in enumerate(tokens):
        if os.path.basename(tok) in SHELLS and tokens[i + 1:i + 2] == ["-c"] and i + 2 < len(tokens):
            merges += find_merges(tokens[i + 2])
            continue
        if os.path.basename(tok) != "gh" or tokens[i + 1:i + 3] != ["pr", "merge"]:
            continue
        selector, repo, disable_auto = None, None, False
        j = i + 3
        while j < len(tokens) and tokens[j] not in SEPARATORS:
            arg = tokens[j]
            nxt = tokens[j + 1] if j + 1 < len(tokens) else None
            if is_redirect(arg):
                j += 2
                continue
            if arg in ("-R", "--repo"):
                repo = nxt
                j += 2
                continue
            if arg.startswith("--repo="):
                repo = arg.split("=", 1)[1]
            elif arg == "--disable-auto":
                disable_auto = True
            elif arg in VALUE_FLAGS:
                j += 2
                continue
            elif not arg.startswith("-") and selector is None:
                selector = arg
            j += 1
        if not disable_auto:  # turning auto-merge off doesn't merge anything
            merges.append((selector, repo))
    return merges


def gh(args, cwd):
    result = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise CheckError(f"`gh {' '.join(args[:2])}` failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout)


def coderabbit_problems(selector, repo, cwd):
    """Return (pr, problems): everything stopping this PR from passing CodeRabbit."""
    view = ["pr", "view", *([selector] if selector else []), *(["--repo", repo] if repo else []),
            "--json", "number,url,state,headRefOid"]
    pr = gh(view, cwd)
    if pr["state"] != "OPEN":
        return pr, []  # gh refuses to merge a closed or merged PR on its own
    owner, name = re.match(r"https://[^/]+/([^/]+)/([^/]+)/pull/", pr["url"]).groups()
    problems = []

    runs = gh(["api", f"repos/{owner}/{name}/commits/{pr['headRefOid']}/check-runs?per_page=100"], cwd)
    runs = [r for r in runs["check_runs"]
            if r["name"] == CHECK_NAME and (r.get("app") or {}).get("slug") == CODERABBIT_APP]
    if not runs:
        problems.append(f"CodeRabbit hasn't reported a check on the latest commit ({pr['headRefOid'][:7]}) yet.")
    else:
        run = max(runs, key=lambda r: r["id"])
        result_line = re.search(r"^Result:.*$", (run.get("output") or {}).get("summary") or "", re.M)
        if run["status"] != "completed":
            problems.append("The CodeRabbit check is still running.")
        elif run["conclusion"] != "success":
            problems.append(f"The CodeRabbit check finished as '{run['conclusion']}', not 'success'.")
        elif result_line and re.search(r"skip|rate.?limit", result_line.group(0), re.I):
            problems.append(f"CodeRabbit didn't review the latest commit ({result_line.group(0).strip()}).")

    cursor, unresolved = None, []
    while True:
        args = ["api", "graphql", "-f", f"query={THREADS_QUERY}", "-f", f"owner={owner}",
                "-f", f"name={name}", "-F", f"number={pr['number']}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = gh(args, cwd)["data"]["repository"]["pullRequest"]
        if cursor is None:
            for review in data["latestReviews"]["nodes"]:
                login = (review.get("author") or {}).get("login") or ""
                if login.startswith(CODERABBIT_APP) and review["state"] == "CHANGES_REQUESTED":
                    problems.append("CodeRabbit's latest review requests changes.")
        for thread in data["reviewThreads"]["nodes"]:
            first = (thread["comments"]["nodes"] or [{}])[0]
            login = (first.get("author") or {}).get("login") or ""
            if login.startswith(CODERABBIT_APP) and not thread["isResolved"]:
                unresolved.append(first.get("url", ""))
        page = data["reviewThreads"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    if unresolved:
        shown = "".join(f"\n    {url}" for url in unresolved[:5])
        more = f"\n    ...and {len(unresolved) - 5} more" if len(unresolved) > 5 else ""
        problems.append(f"{len(unresolved)} CodeRabbit review thread(s) are unresolved:{shown}{more}")
    return pr, problems


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "merge" not in command.lower():
        return
    cwd = payload.get("cwd") or None

    if re.search(r"\bgh\s+api\b", command) and re.search(r"\b(mergePullRequest|enablePullRequestAutoMerge)\b", command):
        deny("Merging through the GraphQL API skips the CodeRabbit check. Use `gh pr merge <number>` instead.")

    try:
        merges = find_merges(command)
    except ValueError:
        if re.search(r"\bgh\s+pr\s+merge\b", command):
            deny("Couldn't parse this command to find which PR it merges. Run `gh pr merge <number>` on its own.")
        return
    rest = re.search(r"\bgh\s+api\b.*?repos/([^/\s'\"]+)/([^/\s'\"]+)/pulls/(\d+)/merge\b", command)
    if rest:
        owner, name, number = rest.groups()
        repo = None if owner.startswith("{") else f"{owner}/{name}"
        merges.append((number, repo))

    for selector, repo in merges:
        label = f"#{selector}" if selector else "the current branch's PR"
        try:
            pr, problems = coderabbit_problems(selector, repo, cwd)
        except (CheckError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError) as err:
            deny(f"Merge blocked: couldn't confirm that {label} passed CodeRabbit ({err}). "
                 "The merge stays blocked until the check can run.")
        if problems:
            deny(f"Merge blocked: PR #{pr['number']} hasn't passed CodeRabbit yet.\n- " + "\n- ".join(problems)
                 + "\nFix or resolve these, let CodeRabbit finish on the newest commit, then merge again. "
                 "Don't work around this check; ask the user if it looks wrong.")


if __name__ == "__main__":
    main()
