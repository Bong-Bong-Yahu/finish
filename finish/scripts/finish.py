#!/usr/bin/env python3
"""finish: Claude may not end a turn that changed things until the work is done.

Five hooks, one file:
  session_start  tells the session the rules and where its goal list lives
  prompt         records what the tree looked like when the person asked (the diff base) and their words;
                 tells the person when a new task starts in a session that already holds a big context
  pre            PreToolUse on edits: a standing decision's guarded file is refused unless the person lifted it
                 in this task's own words; big or hard-to-undo work is refused until its goal list (the spec) exists
                 and, once a goal list exists, until the architecture check passes its ## Approach; past the fresh-context
                 line, the main session's edits wait for one fresh helper to build from the goal list
  post           PostToolUse: once a task passes its token budget, Claude and the person are told to stop and decide
  stop           the stop gate. In order: the goal list (big work), lifted decisions, the failure layer, the slop
                 scan, the project's own checks (exit codes), the architecture check (for edits made around the gate),
                 then a reviewer grounded in the same guides: Sonnet for a small change, Opus for big work. The third
                 rejection, or the block cap, stops it for the person. An objection without a citation is thrown out.

Ceremony follows size. A turn that changes no files is never checked. A small change (under the project's line and
file limits, nothing hard to undo) needs no goal list. Big or hard-to-undo work needs one before its first edit.

The project's settings are <repo>/.claude/finish.json (all optional; DEFAULTS below):
  small            {"lines": N, "files": N}: the most a change may touch and still be small
  hard_to_undo     globs: any change here is big work (migrations, the ship scripts, secrets)
  decisions        path of the decisions registry (DECISIONS.md format in decisions())
  checks           [{"name", "when": [globs], "run": "shell, {files} = the changed files that match"}]
  test_paths       globs of test code: the slop scan skips them, decision guards skip them
  escalate         shell run when it stops for the person, {reason} filled in (shell-quoted)
  task_budget_tokens  per task, in weighted tokens: input + 1.25 cache write + 0.1 cache read + 5 output
  fresh_context_at context tokens: past this, an approved build goes to a fresh helper
  grounding        [{"name", "when": [globs], "version": "shell printing the pinned version or model",
                     "read": [doc URLs or local paths], "look": "what to check there"}]: the vendor's guide per kind
                   of work; the reviewers may fetch only these doc sites
  map              shell that runs the codebase map (`<map> who-uses <thing>`), for the architecture check

State lives in $CLAUDE_PLUGIN_DATA/goals/<session>.{md,json}; nothing is written to the repo.
"""
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

BLOCK_CAP = 8          # blocks per task before it stops and asks the person
REVIEW_CAP = 3         # reviewer rejections per task before it stops and asks the person
ASK_GOAL_CAP = 2       # times we ask for a missing goal list in one task
DIFF_CAP = 60_000      # chars of diff handed to the reviewer inline
REVIEW_TIMEOUT = 600   # seconds for the end review
ARCH_TIMEOUT = 840     # seconds for the architecture check; PreToolUse is given 900
CHECK_TIMEOUT = 180    # seconds per project check
ASKS_CAP = 8_000       # chars of the person's own words handed to the reviewer
FRESH_SESSION_AT = 150_000  # context tokens: a new task past this is told a fresh session is cheaper
SCRATCH = re.compile(r"^/tmp/|/\.claude/projects/[^/]+/memory/")   # memory and scratch files are not work to review
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CODE = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".kt", ".kts", ".java", ".qml", ".sh", ".go", ".rs",
        ".swift", ".c", ".cc", ".cpp", ".h", ".lua", ".rb", ".php"}
DEFAULTS = {
    "small": {"lines": 40, "files": 3},
    "hard_to_undo": [],
    "decisions": "DECISIONS.md",
    "checks": [],
    "test_paths": ["**/test_*.py", "**/*_test.py", "**/conftest.py", "**/tests/**", "**/test/**", "**/*.test.*",
                   "**/*.spec.*", "**/androidTest/**", "**/__tests__/**"],
    "escalate": "",
    "task_budget_tokens": 10_000_000,
    "fresh_context_at": 150_000,
    "grounding": [],
    "map": "",
}

DATA = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or Path.home() / ".claude/plugins/data/finish") / "goals"

RULES = """The finish plugin is on in this session. A turn that changes files ends only after the Stop check passes.

- Ceremony follows size. Talk (no files changed) is never checked. A small change (the project's line and file limits, nothing hard to undo) needs no goal list: it ends after the project's checks and a quick diff review. Big, ambiguous or hard-to-undo work needs its goal list before its first edit; the goal list is the spec.
- A goal list carries an `## Approach` section with three lines: `approach:` (how it will be built), `extends:` (the existing code it builds on, or why nothing fits) and `rejected:` (what else was considered and why not). Before the first edit, an Opus architecture check reads it against the code as it is, the standing decisions and the vendor's guide for this kind of work (the model's prompting guide for prompt work, the framework's docs at the pinned version for code), and sends back a second copy of something that exists, a fix at the wrong layer, or an approach the guide says is wrong. Its objections cite a doc line or a file:line; edits wait until it passes. When the approach changes, it is checked again.
- When the approach passes in a session that already holds a big context, the build goes to one fresh helper (Agent tool, Sonnet) briefed with the goal list alone; this session then checks the helper's work.
- The goal list for this session is {goal}. First line: `# Goal: <the ask, in the person's words>`. Then one line per outcome, written as the thing happening: `- [ ] <outcome> | proof: <the command, record or screen that shows it>`. An item is ticked only after its proof was run: `- [x] <outcome> | proof: ... | result: <what the proof showed>`. An item only the person can unblock is `- [!] <outcome> | needs: <the one thing>`.
- A fix names the layer that failed before anything changes, as a goal-list line `layer: code|state|routing|instruction - <the cause>`. A rule, prompt or instruction is the fix only for an instruction-layer failure.
- A fix starts from the named cause, checks every caller of the code it changes, and removes the whole class of the bug. A special case keyed on one value, a swallowed error, a retry or sleep, a prompt patch, or "for now" is a patch, not a fix. No TODO, stub, placeholder return, mock data in a real path or empty error handler ships as finished work.
- Standing decisions live in the project's decisions file{decisions}. A file a decision guards can only be edited to remove text, unless the person lifted the decision in this task's own words: then add `lifted: "<their exact words>"` under it, and before stopping delete or rewrite the decision (a lasting change) or remove the lifted line (a one-time exception).
- The path a change replaces is deleted in the same change. A passing suite is not proof; proof is the thing working where it runs.
- Before the session may stop, a reviewer reads the person's own words, the goal list, the diff and the final reply, re-runs the proofs it can, and sends the work back if it finds a patch, slop, a fix at the wrong layer, a skipped caller, an old path left behind, a claim its own run does not bear out, work stopped short of the ask, or work the person never asked for. The third time it sends the work back, the work stops and the person decides.
- When the stop is blocked, the reason says what is missing; the work continues from there."""


# ---- state and input --------------------------------------------------------------------------------------------

def read_input():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def paths(sid):
    DATA.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sid or "nosession")
    return DATA / f"{safe}.md", DATA / f"{safe}.json"


def load_state(p):
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_state(p, st):
    p.write_text(json.dumps(st, indent=1))


def git(cwd, *args, timeout=30):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def repo_root(cwd):
    rc, out = git(cwd, "rev-parse", "--show-toplevel")
    return out.strip() if rc == 0 and out.strip() else None


def config(root):
    cfg = json.loads(json.dumps(DEFAULTS))
    if root:
        try:
            cfg.update(json.loads((Path(root) / ".claude/finish.json").read_text()))
        except FileNotFoundError:
            pass
        except Exception as e:
            cfg["_error"] = f".claude/finish.json is not valid JSON ({e})"
    return cfg


def match(path, globs):
    """fnmatch where `**/` may also match nothing, so `**/test_*.py` matches `test_a.py`."""
    for g in globs or []:
        if fnmatch.fnmatch(path, g) or (g.startswith("**/") and fnmatch.fnmatch(path, g[3:])):
            return True
    return False


def norm(s):
    return " ".join(re.sub(r"[\"'`“”‘’]", "", s).lower().split())


# ---- the tree -----------------------------------------------------------------------------------------------------

def snapshot(cwd):
    """A commit-ish for the tree as it is now (staged + unstaged), plus the untracked files."""
    rc, _ = git(cwd, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return None
    _, stash = git(cwd, "stash", "create")
    _, head = git(cwd, "rev-parse", "HEAD")
    _, untracked = git(cwd, "ls-files", "--others", "--exclude-standard")
    return {"base": (stash.strip() or head.strip()), "untracked": sorted(untracked.split())}


def tree_changes(cwd, snap):
    """(diff text, new untracked files) since the snapshot, or ('', []) outside git. Paths are repo-relative."""
    if not snap or not snap.get("base"):
        return "", []
    root = repo_root(cwd) or cwd
    _, diff = git(root, "diff", snap["base"], timeout=60)
    _, untracked = git(root, "ls-files", "--others", "--exclude-standard")
    new = sorted(set(untracked.split()) - set(snap.get("untracked", [])))
    return diff, new


def added_lines(root, diff, new_files):
    """{path: [added line, ...]} from the diff and the new files."""
    out, cur = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif cur and line.startswith("+") and not line.startswith("+++"):
            out.setdefault(cur, []).append(line[1:])
    for f in new_files:
        try:
            p = Path(root) / f
            if p.stat().st_size < 400_000:
                out[f] = p.read_text(errors="replace").splitlines()
        except OSError:
            pass
    return out


def size(diff, new_files, added):
    files = set(re.findall(r"^diff --git a/(\S+) ", diff, re.M)) | set(new_files)
    lines = sum(1 for l in diff.splitlines() if (l[:1] in "+-") and not l.startswith(("+++", "---")))
    lines += sum(len(added.get(f, [])) for f in new_files)
    return files, lines


def triage(cfg, files, lines, guarded):
    """'small' or 'big', with the reason it is big."""
    hard = [f for f in files if match(f, cfg.get("hard_to_undo"))]
    if hard:
        return "big", f"it touches {', '.join(sorted(hard)[:3])}, which is hard to undo"
    if guarded:
        return "big", f"it touches {', '.join(sorted(guarded)[:3])}, which a standing decision guards"
    lim = cfg.get("small") or {}
    if len(files) > lim.get("files", 3):
        return "big", f"it touches {len(files)} files"
    if lines > lim.get("lines", 40):
        return "big", f"it changes {lines} lines"
    return "small", ""


# ---- standing decisions ---------------------------------------------------------------------------------------------

def decisions(text):
    """The registry: `## D<n> <title>` sections, each with `why:`, optional `guards: glob, glob`, `layer: <layer>`,
    and while the person has lifted it, `lifted: "<their exact words>"`."""
    out = {}
    parts = re.split(r"^## (D\d+)\b[ \t]*(.*)$", text or "", flags=re.M)
    for i in range(1, len(parts) - 2, 3):
        did, title, body = parts[i], parts[i + 1].strip(), parts[i + 2]
        fields = dict((m.group(1), m.group(2).strip()) for m in re.finditer(r"^(why|guards|layer|lifted):[ \t]*(.*)$", body, re.M))
        out[did] = {"title": title, "why": fields.get("why", ""), "layer": fields.get("layer", "").lower(),
                    "guards": [g.strip() for g in fields.get("guards", "").split(",") if g.strip()],
                    "lifted": fields.get("lifted", ""),
                    "core": norm(re.sub(r"^lifted:.*$", "", body, flags=re.M))}
    return out


def lifted_ok(dec, asks):
    q = norm(dec.get("lifted", ""))
    return len(q) >= 8 and q in norm(" ".join(asks or []))


def guards_for(path, decs, cfg):
    if match(path, cfg.get("test_paths")):
        return []
    return [(did, d) for did, d in decs.items() if match(path, d["guards"])]


def read_decisions(root, cfg):
    try:
        return (Path(root) / cfg["decisions"]).read_text(errors="replace")
    except Exception:
        return ""


def apply_edit(tool, ti, before):
    """The file's text after this edit, or None when it cannot be told."""
    if tool == "Write":
        return ti.get("content", "")
    edits = ti.get("edits") if tool == "MultiEdit" else [ti] if tool == "Edit" else None
    if edits is None:
        return None
    text = before
    for e in edits:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


def removes_only(before, after):
    """True when every line of `after` was already a line of `before` (as a multiset) and something went."""
    if after is None or len(after) >= len(before):
        return False
    pool = {}
    for l in before.splitlines():
        pool[l] = pool.get(l, 0) + 1
    for l in after.splitlines():
        if pool.get(l, 0) <= 0:
            return False
        pool[l] -= 1
    return True


# ---- the transcript ---------------------------------------------------------------------------------------------

def turn_tool_uses(transcript_path):
    """(tool name, input) for each tool Claude used since the person's last real message."""
    uses = []
    try:
        lines = Path(transcript_path).read_text(errors="replace").splitlines()
    except Exception:
        return uses
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except Exception:
            continue
        msg = o.get("message") or {}
        content = msg.get("content")
        if o.get("type") == "user":
            if isinstance(content, str):
                break
            if isinstance(content, list) and not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                break
        elif o.get("type") == "assistant" and isinstance(content, list):
            uses += [(c.get("name"), c.get("input") or {}) for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
    return uses


def usage_since(transcript_path, offset):
    """(weighted tokens used since byte `offset`, context size of the newest call) for the main chain."""
    calls, last = {}, 0
    try:
        with open(transcript_path, "rb") as f:
            f.seek(offset or 0)
            data = f.read().decode(errors="replace")
    except Exception:
        return 0, 0
    for line in data.splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        m = o.get("message") or {}
        u = m.get("usage") or {}
        if o.get("type") != "assistant" or o.get("isSidechain") or not u or m.get("model") == "<synthetic>":
            continue
        calls[m.get("id") or len(calls)] = u
        last = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    w = sum(u.get("input_tokens", 0) + 1.25 * u.get("cache_creation_input_tokens", 0)
            + 0.1 * u.get("cache_read_input_tokens", 0) + 5 * u.get("output_tokens", 0) for u in calls.values())
    return int(w), last


def file_size(p):
    try:
        return os.path.getsize(p)
    except Exception:
        return 0


# ---- the goal list ----------------------------------------------------------------------------------------------

def parse_goal(text):
    items = []
    for line in text.splitlines():
        m = re.match(r"\s*-\s*\[([ xX!])\]\s*(.+)", line)
        if m:
            items.append((m.group(1).lower(), m.group(2).strip()))
    return items


def say(event, text, user=None):
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if user:
        out["systemMessage"] = user
    print(json.dumps(out))


def block(reason, user=None):
    out = {"decision": "block", "reason": "finish: " + reason}
    if user:
        out["systemMessage"] = user
    print(json.dumps(out))
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "finish: " + reason}}))
    sys.exit(0)


# ---- the gates under the reviewer ----------------------------------------------------------------------------------

SLOP_HARD = [
    (re.compile(r"\b(TODO|FIXME|XXX|HACK)\b"), "a TODO/FIXME left in"),
    (re.compile(r"\bNotImplementedError\b|\bTODO\(\)"), "a stub that raises not-implemented"),
    (re.compile(r"^\s*except\b[^:]*:\s*(pass|\.\.\.)\s*(#.*)?$"), "an except that swallows the error"),
    (re.compile(r"\bcatch\s*(\([^)]*\))?\s*\{\s*\}"), "an empty catch"),
    (re.compile(r"\.catch\(\s*\(?\s*\w*\s*\)?\s*=>\s*(\{\s*\}|null|undefined)\s*\)"), "a promise error dropped"),
]
SLOP_SUSPECT = re.compile(r"\b(mock|mocked|dummy|fake|stub|placeholder|lorem|hard-?coded|sample data|not implemented)\b", re.I)


def slop(cfg, added):
    """(hard hits that block, suspects the reviewer rules on), each 'path: what: the line'."""
    hard, suspects = [], []
    for path, lines in added.items():
        if Path(path).suffix not in CODE or match(path, cfg.get("test_paths")):
            continue
        for i, line in enumerate(lines):
            hit = next((why for rx, why in SLOP_HARD if rx.search(line)), None)
            if not hit and re.match(r"^\s*except\b[^:]*:\s*(#.*)?$", line) and i + 1 < len(lines) \
                    and re.match(r"^\s*(pass|\.\.\.)\s*(#.*)?$", lines[i + 1]):
                hit = "an except that swallows the error"
            if hit:
                hard.append(f"{path}: {hit}: {line.strip()[:120]}")
            elif SLOP_SUSPECT.search(line):
                suspects.append(f"{path}: {line.strip()[:120]}")
    return hard, suspects


def run_checks(root, cfg, files):
    """The project's own checks on the changed files. [(name, exit code, output tail)] for each that failed."""
    env = check_env()
    failed = []
    for c in cfg.get("checks") or []:
        hit = sorted(f for f in files if not os.path.isabs(f) and match(f, c.get("when") or ["*"]) and (Path(root) / f).exists())
        if not hit:
            continue
        cmd = c.get("run", "").replace("{files}", " ".join(shlex.quote(f) for f in hit))
        try:
            r = subprocess.run(["bash", "-c", cmd], cwd=root, env=env, capture_output=True, text=True, timeout=CHECK_TIMEOUT)
            if r.returncode != 0:
                failed.append((c.get("name", cmd), r.returncode, "\n".join((r.stdout + r.stderr).strip().splitlines()[-30:])))
        except subprocess.TimeoutExpired:
            failed.append((c.get("name", cmd), "timeout", f"did not finish in {CHECK_TIMEOUT}s"))
    return failed


# ---- grounding: the vendor's guide for the work --------------------------------------------------------------------

def check_env():
    """The environment a check or a version command runs in: no model keys, so it can never spend."""
    env = {k: v for k, v in os.environ.items() if not re.search(r"(_API_KEY|_AUTH_TOKEN)$", k)}
    env["FINISH_CHECK"] = "1"
    return env


def grounding_for(root, cfg, files):
    """Each grounding entry whose globs match a changed or planned file, with the version the repo pins, read now."""
    out = []
    for g in cfg.get("grounding") or []:
        hit = sorted(f for f in files if match(f, g.get("when")))
        if not hit:
            continue
        version = ""
        if g.get("version"):
            try:
                r = subprocess.run(["bash", "-c", g["version"]], cwd=root, env=check_env(), capture_output=True,
                                   text=True, timeout=15)
                version = " ".join(r.stdout.split())[:300] or f"(the version command printed nothing: {r.stderr.strip()[:150]})"
            except Exception as e:
                version = f"(the version command failed: {type(e).__name__})"
        out.append(dict(g, files=hit, version_now=version))
    return out


def doc_hosts(guides):
    hosts = set()
    for g in guides:
        for r in g.get("read") or []:
            m = re.match(r"https?://([^/]+)", r)
            if m:
                hosts.add(m.group(1).lower())
    return sorted(hosts)


def local_dirs(guides):
    dirs = set()
    for g in guides:
        for r in g.get("read") or []:
            if not re.match(r"https?://", r):
                p = Path(os.path.expanduser(r))
                if p.exists():
                    dirs.add(str(p if p.is_dir() else p.parent))
    return sorted(dirs)


def unread(read, guides):
    """The guides a verdict's `read` list shows no source of (a page on the guide's site, or a file under its local
    path)."""
    def seen(src):
        m = re.match(r"https?://([^/]+)", src)
        if m:
            return any(m.group(1).lower() in r.lower() for r in read)
        d = os.path.expanduser(src)
        return any(os.path.expanduser(r).startswith(d) for r in read)
    return [g for g in guides if not any(seen(src) for src in g.get("read") or [])]


def grounded(run, guides):
    """Run a reviewer; a pass that read none of a guide's sources is run once more, told which it skipped."""
    v, err = run("")
    missed = unread(v["read"], guides) if v and v["ok"] else []
    if missed:
        names = "; ".join(f"{g.get('name')}: {', '.join(g.get('read') or [])}" for g in missed)
        v, err = run(f"\n\nYOUR LAST VERDICT PASSED IT WITHOUT READING THESE GUIDES. Read them now, then decide: {names}")
        if v and v["ok"] and unread(v["read"], guides):
            v["ungrounded"] = True
    return v, err


def guides_text(guides):
    if not guides:
        return "(no vendor guide is mapped for these files)"
    return "\n".join(
        f"- {g.get('name', '?')} (for {', '.join(g['files'][:6])}). Pinned in the repo now: {g['version_now'] or 'n/a'}. "
        f"Read: {', '.join(g.get('read') or [])}. Look for: {g.get('look') or 'what it says about this change'}"
        for g in guides)


def planned_files(text, root):
    """Repo paths a goal list names (existing or to be made), for picking its guides before anything is built."""
    out = set()
    for tok in re.findall(r"(?<![\w/:.~-])[\w.*-]+(?:/[\w.*-]+)*", text or ""):
        tok = tok.strip(".")
        if "/" in tok or (re.search(r"\.[A-Za-z]\w*$", tok) and (Path(root) / tok).exists()):
            out.add(tok)
    return out


# ---- citations: an objection points at the line it stands on --------------------------------------------------------

def valid_cites(cites, root, hosts, dirs):
    """The citations that hold: a URL on one of the guide's doc sites with a quoted sentence, or a file (and line) that
    exists in the repo or in a local guide."""
    good = []
    for c in cites or []:
        c = str(c)
        url = re.search(r"https?://([^/\s\"'<>)]+)", c)
        if url:
            host = url.group(1).lower()
            quoted = re.search(r"[\"“](.{12,}?)[\"”]", c)
            if quoted and any(host == h or host.endswith("." + h) for h in hosts):
                good.append(c)
            continue
        m = re.search(r"((?:~/|/)?[\w.\-]+(?:/[\w.\-]+)*\.\w+)(?::(\d+))?", c)
        if not m:
            continue
        cands = [Path(root) / m.group(1)] + [Path(os.path.expanduser(m.group(1)))]
        cands += [Path(d) / m.group(1) for d in dirs]
        f = next((p for p in cands if p.is_file() and (p.resolve().is_relative_to(Path(root).resolve())
                                                       or any(str(p.resolve()).startswith(d) for d in dirs))), None)
        if not f:
            continue
        if m.group(2):
            try:
                if int(m.group(2)) > len(f.read_text(errors="replace").splitlines()):
                    continue
            except OSError:
                continue
        good.append(c)
    return good


# ---- the reviewers ----------------------------------------------------------------------------------------------------

def laws_for(cwd, files):
    """Every CLAUDE.md that governs a changed file: the repo root's and each folder's on the way down to it."""
    found = set()
    for f in files:
        parts = Path(f).parts[:-1]
        for i in range(len(parts) + 1):
            p = Path(*parts[:i], "CLAUDE.md") if i else Path("CLAUDE.md")
            if (Path(cwd) / p).is_file():
                found.add(str(p))
    return sorted(found, key=lambda p: (p.count("/"), p)) or ["CLAUDE.md"]


NO_WRITES = ("Never run anything that writes, commits, pushes, ships, deploys, installs, restarts a service, sends a "
             "message, or calls a paid model.")
VERDICT = ('End your answer with exactly one line of JSON: {"ok": true, "read": ["<each page and file you read>"]} or '
           '{"ok": false, "reason": "<what must change, under 600 characters>", "cites": ["<a doc URL followed by the '
           'exact sentence in double quotes, or a repo file:line>", ...], "read": [...]}. An objection without a '
           'citation does not count: the plugin throws it out.')


def run_reviewer(prompt, model, cwd, hosts, dirs, timeout):
    """One headless Claude Code run that may read, grep, query and fetch the guide's doc sites, never write.
    (verdict dict, '') or (None, why it gave none)."""
    env = dict(os.environ, FINISH_REVIEWER="1")
    cmd = ["claude", "-p", prompt, "--model", model, "--setting-sources", "project",
           "--no-session-persistence", "--output-format", "json",
           "--allowedTools", "Read", "Grep", "Glob", "Bash", "mcp__claude_ai_Aegis_Database__*",
           *[f"WebFetch(domain:{h})" for h in hosts],
           "--disallowedTools", "Edit", "Write", "NotebookEdit", "Agent", "WebSearch",
           "Bash(git commit:*)", "Bash(git push:*)", "Bash(git reset:*)", "Bash(git checkout:*)",
           "Bash(git stash:*)", "Bash(git rebase:*)", "Bash(git merge:*)", "Bash(rm:*)",
           "Bash(*ship.sh*)", "Bash(*land.sh*)", "Bash(*deploy*)", "Bash(*systemctl*)",
           "Bash(adb install:*)", "Bash(*adb*install*)", "Bash(ssh:*)", "Bash(curl:*)", "Bash(wget:*)"]
    for d in dirs:
        cmd += ["--add-dir", d]
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        result = json.loads(r.stdout).get("result", "")
    except Exception as e:
        return None, f"reviewer did not run ({type(e).__name__})"
    for line in reversed(result.strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{"):
            try:
                v = json.loads(line)
                return {"ok": bool(v.get("ok")), "reason": v.get("reason", ""), "cites": v.get("cites") or [],
                        "read": v.get("read") or []}, ""
            except Exception:
                break
    return None, "reviewer answer had no verdict line"


def approach_of(text):
    """The goal list's `## Approach` section, or '' when it has none."""
    m = re.search(r"^##\s*Approach\s*$(.*?)(?=^#|\Z)", text or "", re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def approach_missing(appr):
    return [k for k in ("approach", "extends", "rejected") if not re.search(rf"^\s*(-\s*)?{k}:\s*\S", appr, re.M | re.I)]


def arch_check(root, cfg, goal_text, asks, guides, decisions_file, tail=""):
    """Before anything is built: is this the right approach, against the code, the standing decisions and the vendor's
    guide for this work?"""
    mapcmd = cfg.get("map") or ""
    prompt = f"""You are the architecture check. A Claude Code session is about to build something; nothing is built yet. Decide whether its approach is the right one before any code is written.

Repo: {root}. The laws: {', '.join(laws_for(root, planned_files(goal_text, root)))}.{f' Standing decisions: {decisions_file}.' if decisions_file else ''}

WHAT THE PERSON ASKED (their own words, oldest first):
{asks or '(not recorded)'}

THE GOAL LIST AND ITS APPROACH (written by the session):
{goal_text}

THE VENDOR'S GUIDE FOR THIS WORK (picked from the files it touches and the versions the repo pins; read these, fetching the pages and following an index to the page that covers this change):
{guides_text(guides)}

Check three things, in this order:
1. THE CODE AS IT IS. Find what already does part of this: {f'the codebase map (`{mapcmd} who-uses <thing>`, `{mapcmd} --help`), ' if mapcmd else ''}git grep, reading the code. Docs in the repo may be stale; the code decides.
2. THE STANDING DECISIONS and the laws.
3. THE GUIDE: what the vendor says for this model or version about this kind of change.

Send it back (ok=false) only for:
A. SECOND COPY: it builds a new path beside code that already does this, or does not extend what it should.
B. AGAINST A DECISION or a law the person did not lift in their words above.
C. AGAINST THE GUIDE: the vendor's guide says this approach is wrong, or worse than another, for this model or version.
D. WRONG LAYER: it fixes a failure at a layer other than the one that failed (code, state/data, routing, instruction); a rule or prompt line answering a code, data or routing failure.
E. MISREAD: the approach would not deliver what the person's words ask.
F. OVERBUILT: it builds more than the ask.
Not for style, naming, or details the build can settle. {NO_WRITES}

{VERDICT}{tail}"""
    return run_reviewer(prompt, "opus", root, doc_hosts(guides), local_dirs(guides), ARCH_TIMEOUT)


def review(cwd, tier, goal_text, reply, diff, new_files, base, asks, suspects, decisions_file, outside, guides, tail=""):
    if len(diff) > DIFF_CAP:
        diff_part = f"(The diff is {len(diff)} characters; run `git diff {base}` yourself, in parts.)"
    else:
        diff_part = diff or "(no tracked-file diff)"
    files = set(re.findall(r"^diff --git a/(\S+) ", diff, re.M)) | set(new_files)
    if tier == "small":
        head = ("This is a SMALL change, so there is no goal list: read the diff against the person's words and the "
                "laws, run a quick proof if one is cheap, and pass it unless one of the reasons below is plainly there.")
        model = "sonnet"
    else:
        head = "This is BIG work. Check it fully."
        model = "opus"
    appr = approach_of(goal_text)
    prompt = f"""You are the finish reviewer. A Claude Code session wants to end its work. Decide if it is really done. {head}

Repo: {cwd}. Read these laws first; each binds the files under its folder: {', '.join(laws_for(cwd, files))}.{f' The standing decisions are in {decisions_file}; a change that goes against one the person did not lift in their words below is a broken law.' if decisions_file else ''}

WHAT THE PERSON ASKED (their own words, oldest first; this is the ask, not the goal list):
{asks or '(not recorded)'}

GOAL LIST (written by the session, from its own reading of the ask):
{goal_text or '(none: a small change)'}

FINAL REPLY TO THE PERSON:
{reply[:6000]}

DIFF since the person asked (base {base}):
{diff_part}

NEW UNTRACKED FILES: {', '.join(new_files[:50]) or 'none'}
FILES EDITED OUTSIDE THIS REPO (read them yourself): {', '.join(outside[:30]) or 'none'}
SLOP SUSPECTS the scan found in added code (rule on each: real mock data, a stub or placeholder in a real path is SLOP; a word in a name, a test seam or a UI placeholder attribute is not): {'; '.join(suspects[:30]) or 'none'}

THE VENDOR'S GUIDE FOR WHAT THIS CODE TOUCHES (read the pages for the parts the diff changes; judge the code by what they say for the pinned version or model):
{guides_text(guides)}

CHECK, DON'T TRUST. For each ticked item, run its proof yourself when you can do it without changing anything: a grep, a file read, `git show`, a read-only query, a GET, a test file that makes no model calls, reading a screenshot it names. Compare what you saw with the item's `result:`. {NO_WRITES} For those, judge the evidence in the reply and the diff. When something is wrong, find the cause in the code and name it: file, line, and what the code does there.

Send it back (ok=false) for any of these, and only these:
0. LAW BROKEN: the change breaks a rule in one of the laws or a standing decision above (name the file and the rule).
1. PATCH, not a fix: a special case keyed on one value, a swallowed or silenced error, a retry/sleep/timeout bump standing in for a cause, a prompt sentence patching model behaviour, a flag-gated second path, "for now"/TODO/temporary with no named proper fix.
2. WRONG LAYER: a fix that names no failed layer (code, state/data, routing, instruction) in the goal list, names one the evidence does not show, or answers a code, data or routing failure by adding a rule, prompt line or instruction.
3. SLOP: work called finished at demo quality: a placeholder or hardcoded return, a stub, mock or sample data in a real path, an empty or swallowing error handler, a new test that only checks the happy path of something with failure modes, UI or output that only works for the one example tried.
4. AGAINST THE GUIDE: the code does what the vendor's guide above says not to do for this version or model.
5. SKIPPED CALLER: a changed function, field, route or format has other callers (grep for them) that this change leaves broken or unchanged.
6. OLD PATH LEFT: the thing this change replaces is still on disk (dead code, shim, doc describing the old way).
7. UNPROVEN: an item is ticked, or the reply says it works, and your own run of the proof shows otherwise, or the result is only "tests pass", only the code's own reply, or nothing that shows the thing working where it runs.
8. STOPPED SHORT: the reply hands the person a remainder (next steps, "you can", "ready to land", a list of things not done) that was inside the ask and not marked [!] with a real need.
9. GOAL MISREAD: the goal list's outcomes do not match what the person's own words asked.
10. NOT AS APPROVED: the goal list's Approach was checked before the build{' (below)' if appr else ''}; what was built goes a different way without saying why.
11. OVERREACH: the change builds something the person did not ask for (a new feature, plugin, job, rule or refactor outside their words), or the reply pushes them toward it. The fix is to remove it, never to add more.
{f'''
THE APPROVED APPROACH:
{appr}
''' if appr else ''}
Never send it back to do more than the person asked. Do not send it back for style or naming. Be concrete: name the file and line and what must change.

{VERDICT}{tail}"""
    return run_reviewer(prompt, model, cwd, doc_hosts(guides), local_dirs(guides), REVIEW_TIMEOUT)


# ---- the hooks ------------------------------------------------------------------------------------------------------

def session_start(inp):
    goal, _ = paths(inp.get("session_id"))
    root = repo_root(inp.get("cwd") or os.getcwd())
    cfg = config(root)
    dpath = root and Path(root) / cfg["decisions"]
    where = f" ({dpath})" if dpath and dpath.is_file() else ""
    say("SessionStart", RULES.format(goal=goal, decisions=where))


def on_prompt(inp):
    goal, statef = paths(inp.get("session_id"))
    st = load_state(statef)
    cwd = inp.get("cwd") or os.getcwd()
    user = None
    if not goal.exists():          # no task running: this prompt starts the next one
        if st.get("tasks"):
            _, context = usage_since(inp.get("transcript_path", ""), st.get("tx_offset", 0))
            if context >= FRESH_SESSION_AT:
                user = (f"finish: this session already holds {context // 1000}k tokens of earlier work. A new task "
                        "costs less and stays sharper in a fresh session (/clear).")
        st["snap"] = snapshot(cwd)
        st["tx_offset"] = file_size(inp.get("transcript_path", ""))
        for k in ("asked_goal", "blocks", "review_fails", "arch_fails"):
            st[k] = 0
        st["asks"] = []
        for k in ("reviewed", "escalated", "budget_told", "arch"):
            st.pop(k, None)
    words = (inp.get("prompt") or "").strip()
    if words:                      # the person's own words: a new ask, or a follow-up inside this one
        st["asks"] = (st.get("asks") or []) + [words]
    save_state(statef, st)
    if user:
        print(json.dumps({"systemMessage": user}))


def pre(inp):
    tool, ti = inp.get("tool_name"), inp.get("tool_input") or {}
    if tool not in EDIT_TOOLS:
        return
    cwd = inp.get("cwd") or os.getcwd()
    root = repo_root(cwd)
    target = ti.get("file_path") or ti.get("notebook_path") or ""
    if not root or not target:
        return
    rel = os.path.relpath(os.path.join(cwd, target), root)
    if rel.startswith(".."):
        return
    goal, statef = paths(inp.get("session_id"))
    st = load_state(statef)
    cfg = config(root)
    asks = st.get("asks") or []
    decs = decisions(read_decisions(root, cfg))
    try:
        before = (Path(root) / rel).read_text(errors="replace")
    except OSError:
        before = ""
    after = apply_edit(tool, ti, before)

    if rel == cfg["decisions"] and after is not None:      # the registry: a standing decision changes only on his word
        now = decisions(after)
        for did, d in decs.items():
            if (did not in now or now[did]["core"] != d["core"]) and not lifted_ok(d, asks):
                deny(f"{did} ({d['title']}) is a standing decision and only the person changes it. If their own words in "
                     f"this task change it, first add a line `lifted: \"<their exact words>\"` under {did}, then edit it.")
    for did, d in guards_for(rel, decs, cfg):
        if not lifted_ok(d, asks) and not removes_only(before, after):
            deny(f"{rel} is guarded by standing decision {did}: {d['title']}. Why: {d['why'][:400]} This edit may only "
                 f"remove text. If the person's own words in this task lift {did}, add under it in {cfg['decisions']} a line "
                 f"`lifted: \"<their exact words>\"`. Otherwise fix the failure where it happened (name the layer).")

    if goal.exists():
        if st.get("escalated"):
            deny("this task stopped for the person: wait for their answer before editing.")
        text = goal.read_text(errors="replace")
        a = approve(root, cfg, st, statef, goal, text, asks, decs, cwd, inp, planned_files(text, root) | {rel}, pre=True)
        if not a["ok"]:
            deny(a["reason"] if a.get("missing") else arch_reason(a))
        if a.get("handoff") and not inp.get("agent_id"):
            deny(f"the approach passed, and this session already holds {a.get('context', 0) // 1000}k tokens. Build it in "
                 "fresh context: hand the work to one helper (the Agent tool, model sonnet) whose whole brief is "
                 f"\"Build what {goal} says, following its ## Approach. Tick each item only after its proof ran, with "
                 "what it showed.\" Its edits go through; then read what it did and finish here.")
        return
    if match(rel, cfg.get("hard_to_undo")) or guards_for(rel, decs, cfg):
        deny(f"{rel} is hard to undo, so this is big work: write the goal list {goal} first (it is the spec: the ask in "
             "their words, each outcome with its proof), then make the edit.")
    diff, new = tree_changes(cwd, st.get("snap"))
    files, lines = size(diff, new, added_lines(root, diff, new))
    tier, why = triage(cfg, files | {rel}, lines, [])
    if tier == "big":
        deny(f"this has grown into big work ({why}): write the goal list {goal} first (it is the spec: the ask in "
             "their words, each outcome with its proof), then go on.")


def post(inp):
    goal, statef = paths(inp.get("session_id"))
    st = load_state(statef)
    if st.get("budget_told") or st.get("escalated"):
        return
    cfg = config(repo_root(inp.get("cwd") or os.getcwd()))
    budget = int(cfg.get("task_budget_tokens") or 0)
    if not budget:
        return
    used, _ = usage_since(inp.get("transcript_path", ""), st.get("tx_offset", 0))
    if used < budget:
        return
    st["budget_told"] = used
    save_state(statef, st)
    m = f"{used / 1e6:.1f}M of its {budget / 1e6:.1f}M token budget"
    say("PostToolUse",
        f"finish: this task has used {m}. Stop starting new work: finish the step you are on, then tell the person "
        "in two sentences where it stands and what is left, and ask whether to go on.",
        user=f"finish: this task has used {m}; Claude will stop and ask you whether to go on.")


def note(goal, text):
    if goal.exists():
        with goal.open("a") as f:
            f.write(f"\n<!-- finish: {text} -->\n")


def arch_reason(a):
    return (f"the architecture check sent the approach back: {a['reason']} Citations: {'; '.join(a['cites'][:5])}. "
            "Change the approach (or the plan) in the goal list's ## Approach, then go on; it is checked again.")


def approve(root, cfg, st, statef, goal, text, asks, decs, cwd, inp, files, pre=False):
    """The architecture check's verdict on this goal list's approach, run once per version of it."""
    appr = approach_of(text)
    miss = approach_missing(appr)
    if miss:
        return {"ok": False, "missing": True, "reason":
                f"the goal list {goal} needs an `## Approach` section before the build, with "
                + ", ".join(f"`{k}:`" for k in miss) + " lines: how it will be built, the existing code it extends (or "
                "why nothing fits), and what it rejected and why. The architecture check reads it before the first edit."}
    key = hashlib.sha256((text.splitlines()[0] if text else "").encode() + appr.encode()).hexdigest()
    a = st.get("arch") or {}
    if a.get("key") == key:
        return a
    guides = grounding_for(root, cfg, files)
    v, err = grounded(lambda tail: arch_check(root, cfg, text, "\n---\n".join(asks)[-ASKS_CAP:], guides,
                                              cfg["decisions"] if decs else "", tail), guides)
    a = {"key": key, "ok": True, "reason": "", "cites": []}
    if v is None:
        note(goal, f"architecture check: {err}; the build went ahead unchecked")
    elif v["ok"] and (not v["read"] or v.get("ungrounded")):
        note(goal, "architecture check passed twice without reading the vendor's guide: the approach is ungrounded")
    elif not v["ok"]:
        good = valid_cites(v["cites"], root, doc_hosts(guides), local_dirs(guides))
        if good:
            a.update(ok=False, reason=v["reason"], cites=good)
        else:
            note(goal, f"architecture check objection thrown out (no citation that holds): {v['reason'][:300]}")
    if a["ok"] and not inp.get("agent_id"):
        _, context = usage_since(inp.get("transcript_path", ""), 0)
        a.update(context=context, handoff=context >= int(cfg.get("fresh_context_at") or 0) > 0)
    st["arch"] = a
    if not a["ok"]:
        st["arch_fails"] = st.get("arch_fails", 0) + 1
    save_state(statef, st)
    if not a["ok"] and st["arch_fails"] >= REVIEW_CAP:
        escalate(st, statef, goal, cfg, cwd, f"the architecture check sent the approach back {REVIEW_CAP} times, last: "
                 f"{a['reason']}", pre=pre)
    return a


def escalate(st, statef, goal, cfg, cwd, why, pre=False):
    """Stop spinning: the session tells the person, the person is tapped, the next stop passes."""
    st["escalated"] = why
    save_state(statef, st)
    if goal.exists():
        with goal.open("a") as f:
            f.write(f"\n<!-- finish: stopped for the person: {why} -->\n")
        goal.rename(goal.with_suffix(".escalated.md"))
    if cfg.get("escalate"):
        try:
            subprocess.run(["bash", "-c", cfg["escalate"].replace("{reason}", shlex.quote(("Stuck: " + why)[:190]))],
                           cwd=cwd, capture_output=True, timeout=10)
        except Exception:
            pass
    msg = (f"stop working on this now: {why}. Write the person a short message: what is done, what is still "
           "objected to and why, and the one decision you need from them. Then stop.")
    if pre:
        deny(msg)
    block(msg, user=f"finish: stopped for you. {why[:300]}")


def stop(inp):
    cwd = inp.get("cwd") or os.getcwd()
    goal, statef = paths(inp.get("session_id"))
    st = load_state(statef)
    if st.get("escalated"):
        return
    root = repo_root(cwd) or cwd
    cfg = config(repo_root(cwd))
    diff, new_files = tree_changes(cwd, st.get("snap"))
    added = added_lines(root, diff, new_files)
    files, lines = size(diff, new_files, added)
    uses = turn_tool_uses(inp.get("transcript_path", ""))
    edited_paths = [ti.get("file_path") or ti.get("notebook_path") or "" for n, ti in uses if n in EDIT_TOOLS]
    outside = sorted({p for p in edited_paths if p and not os.path.abspath(p).startswith(str(root) + os.sep)
                      and not SCRATCH.search(os.path.abspath(p)) and not os.path.abspath(p).startswith(str(DATA))})
    files = files | set(outside)    # outside git, so harder to undo: they count toward the size by their edits
    lines += sum(len((ti.get("new_string") or ti.get("content") or "").splitlines())
                 for n, ti in uses if n in EDIT_TOOLS and (ti.get("file_path") or "") in outside)
    edited = bool(files)
    if not goal.exists() and not edited:
        return

    decs_text = read_decisions(root, cfg)
    decs = decisions(decs_text)
    guarded = sorted({f for f in files if guards_for(f, decs, cfg)})
    tier, why_big = triage(cfg, files, lines, guarded)

    def capped(reason):
        st["blocks"] = st.get("blocks", 0) + 1
        save_state(statef, st)
        if st["blocks"] > BLOCK_CAP:
            escalate(st, statef, goal, cfg, cwd, f"blocked {BLOCK_CAP} times, last: {reason[:300]}")
        block(reason)

    if cfg.get("_error"):
        capped(cfg["_error"])
    text = ""
    if goal.exists():
        text = goal.read_text(errors="replace")
        items = parse_goal(text)
        if not items:
            capped(f"{goal} has no `- [ ]` items. List each outcome of the ask with its proof.")
        open_items = [t for s, t in items if s == " "]
        if open_items:
            capped("not done yet: " + "; ".join(i.split("|")[0].strip() for i in open_items[:8]))
        no_result = [t for s, t in items if s == "x" and "result:" not in t]
        if no_result:
            capped("ticked without a result (run the proof, write what it showed): "
                   + "; ".join(i.split("|")[0].strip() for i in no_result[:8]))
        no_need = [t for s, t in items if s == "!" and "needs:" not in t]
        if no_need:
            capped("a [!] item must say `needs:` the one thing only the person can do: "
                   + "; ".join(i.split("|")[0].strip() for i in no_need[:8]))
    elif tier == "big":
        st["asked_goal"] = st.get("asked_goal", 0) + 1
        save_state(statef, st)
        if st["asked_goal"] > ASK_GOAL_CAP:
            escalate(st, statef, goal, cfg, cwd, f"big work ({why_big}) and still no goal list after {ASK_GOAL_CAP} asks")
        block(f"this is big work ({why_big}) and there is no goal list. Write {goal} (format in the session rules), "
              "then carry every item to its proof.")

    still_lifted = [did for did, d in decs.items() if d["lifted"]]
    if still_lifted:
        capped(f"{', '.join(still_lifted)} still carry a `lifted:` line in {cfg['decisions']}. If the person changed the "
               "decision for good, delete or rewrite it to what stands now; if it was a one-time exception, remove the "
               "lifted line.")
    instr = [f for f in guarded if added.get(f) and any(d["layer"] == "instruction" for _, d in guards_for(f, decs, cfg))]
    if instr and not re.search(r"^\s*(-\s*)?layer:\s*instruction\b", text, re.M | re.I):
        capped(f"{', '.join(instr)} carry instructions, which fix only an instruction-layer failure. Add to the goal "
               "list `layer: instruction - <the evidence that the failure was the instruction>`, or move the fix to "
               "the layer that failed.")
    hard, suspects = slop(cfg, added)
    if hard:
        capped("slop in finished code (make it real, or if a hit is not a stub, put `slop-ok: <file> <why>` in the "
               "goal list for the reviewer): " + "; ".join(hard[:10]))
    failed = run_checks(root, cfg, files)
    if failed:
        capped("the project's checks failed: " + " | ".join(f"{n} (exit {c}): {out[-1500:]}" for n, c, out in failed))

    if text:
        a = approve(root, cfg, st, statef, goal, text, st.get("asks") or [], decs, cwd, inp,
                    planned_files(text, root) | files)
        if not a["ok"]:
            capped(a["reason"] if a.get("missing") else arch_reason(a))

    key = hashlib.sha256((text + diff + "".join(new_files) + "".join(outside)).encode()).hexdigest()
    if st.get("reviewed") != key:
        asks = "\n---\n".join(st.get("asks") or [])[-ASKS_CAP:]
        dfile = cfg["decisions"] if decs else ""
        guides = grounding_for(root, cfg, files)
        def run(tail):
            return review(cwd, tier, text, inp.get("last_assistant_message", ""), diff, new_files,
                          (st.get("snap") or {}).get("base", ""), asks, suspects, dfile, outside, guides, tail)
        v, err = grounded(run, guides) if tier == "big" else run("")
        if v and v.get("ungrounded"):
            note(goal, "the end review passed twice without reading the vendor's guide")
        st["reviewed"] = key
        save_state(statef, st)
        if v is None:
            note(goal, f"{err}; let it stop unreviewed")
        elif not v["ok"]:
            good = valid_cites(v["cites"], root, doc_hosts(guides), local_dirs(guides))
            if not good:
                note(goal, f"review objection thrown out (no citation that holds): {v['reason'][:300]}")
            else:
                st["review_fails"] = st.get("review_fails", 0) + 1
                save_state(statef, st)
                reason = f"{v['reason']} Citations: {'; '.join(good[:5])}"
                if st["review_fails"] >= REVIEW_CAP:
                    escalate(st, statef, goal, cfg, cwd, f"the reviewer sent it back {REVIEW_CAP} times, last: {reason}")
                capped("the reviewer sent it back: " + reason)
    if goal.exists():
        goal.rename(goal.with_suffix(".done.md"))
    st["tasks"] = st.get("tasks", 0) + 1
    st.pop("reviewed", None)
    save_state(statef, st)


if __name__ == "__main__":
    if os.environ.get("FINISH_REVIEWER") or os.environ.get("FINISH_CHECK"):   # the reviewer's or a check's own session
        sys.exit(0)
    inp = read_input()
    {"session_start": session_start, "prompt": on_prompt, "pre": pre, "post": post, "stop": stop}[sys.argv[1]](inp)
