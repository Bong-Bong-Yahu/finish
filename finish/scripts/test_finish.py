"""The gates of finish, without a model call (the reviewer is stubbed): python3 test_finish.py"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
data = tempfile.mkdtemp()
os.environ["CLAUDE_PLUGIN_DATA"] = data
import finish  # noqa: E402

GIT = ["-c", "user.email=t@t", "-c", "user.name=t"]


def make_repo(cfg=None, decisions=""):
    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", repo])
    Path(repo, "a.py").write_text("x = 1\n")
    Path(repo, "prompt.txt").write_text("Be brief.\nBe kind.\n")
    Path(repo, "mig").mkdir()
    Path(repo, ".claude").mkdir()
    Path(repo, ".claude/finish.json").write_text(json.dumps(cfg or {}))
    if decisions:
        Path(repo, "DECISIONS.md").write_text(decisions)
    subprocess.run(["git", "-C", repo, "add", "."])
    subprocess.run(["git", "-C", repo, *GIT, "commit", "-qm", "i"])
    return repo


def call(fn, inp):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            fn(dict(inp))
    except SystemExit:
        pass
    return out.getvalue()


reviews, archs = [], []


def stub(ok, reason="", cites=("a.py:1",)):
    def r(*a):
        reviews.append({"tier": a[1], "asks": a[7], "suspects": a[8], "guides": a[11]})
        return {"ok": ok, "reason": reason, "cites": list(cites), "read": ["a.py"]}, ""
    finish.review = r


def arch(ok, reason="", cites=("a.py:1",), read=("a.py",)):
    def r(*a):
        archs.append({"text": a[2], "guides": a[4]})
        return {"ok": ok, "reason": reason, "cites": list(cites), "read": list(read)}, ""
    finish.arch_check = r


arch(True)
APPROACH = "\n## Approach\napproach: change it in place\nextends: a.py\nrejected: a new module (a second copy)\n"


DEC = """# Decisions
## D1 The prompt stays short
why: the long one made him stiff (A/B 13-7).
guards: prompt.txt
layer: instruction
"""

# ---- 1. triage: talk is free, small needs no goal list, big needs one first ----------------------------------------
repo = make_repo({"small": {"lines": 10, "files": 2}, "hard_to_undo": ["mig/*"]})
s1 = {"session_id": "small", "cwd": repo, "transcript_path": "", "last_assistant_message": "done"}
finish.on_prompt(dict(s1, prompt="make x 2"))
stub(True)
assert call(finish.stop, s1) == "" and not reviews, "no change: may stop, nothing reviewed"
Path(repo, "a.py").write_text("x = 2\n")
assert call(finish.stop, s1) == "", "small change: no goal list needed"
assert reviews[-1]["tier"] == "small" and reviews[-1]["asks"] == "make x 2", "small change gets the light review with his words"

s2 = dict(s1, session_id="big")
finish.on_prompt(dict(s2, prompt="rewrite it"))
Path(repo, "b.py").write_text("".join(f"y{i} = {i}\n" for i in range(30)))
out = call(finish.stop, s2)
assert "big work" in out and "no goal list" in out, out
w = {"tool_name": "Write", "tool_input": {"file_path": str(Path(repo, "c.py")), "content": "z = 1\n"}}
assert "grown into big work" in call(finish.pre, dict(s2, **w)), "the next edit of big work waits for the spec"
w_mig = {"tool_name": "Write", "tool_input": {"file_path": str(Path(repo, "mig/001.sql")), "content": "select 1;\n"}}
s3 = dict(s1, session_id="mig")
finish.on_prompt(dict(s3, prompt="add a migration"))
assert "hard to undo" in call(finish.pre, dict(s3, **w_mig)), "hard-to-undo work needs the goal list before the first edit"
goal3, _ = finish.paths("mig")
goal3.write_text("# Goal: add a migration\n- [ ] it runs | proof: psql\n" + APPROACH)
assert call(finish.pre, dict(s3, **w_mig)) == "", "with the spec written the edit goes through"
goal3.unlink()

# ---- 2. decisions: a guarded file only shrinks, unless his words lift it; the lift must not outlive the task -----
repo = make_repo(decisions=DEC)
s = {"session_id": "dec", "cwd": repo, "transcript_path": "", "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="bram keeps using em dashes, fix it"))
add = {"tool_name": "Edit", "tool_input": {"file_path": str(Path(repo, "prompt.txt")), "old_string": "Be kind.\n",
                                           "new_string": "Be kind.\nNever use em dashes.\n"}}
out = call(finish.pre, dict(s, **add))
assert "standing decision D1" in out and "stiff" in out and '"deny"' in out, out
cut = {"tool_name": "Edit", "tool_input": {"file_path": str(Path(repo, "prompt.txt")), "old_string": "Be kind.\n", "new_string": ""}}
goal, _ = finish.paths("dec")
goal.write_text("# Goal: x\n- [ ] y | proof: z\n" + APPROACH)
assert call(finish.pre, dict(s, **cut)) == "", "deleting text from a guarded file is allowed"
fake = {"tool_name": "Edit", "tool_input": {"file_path": str(Path(repo, "DECISIONS.md")),
                                            "old_string": "guards: prompt.txt\n", "new_string": ""}}
assert "only the person changes it" in call(finish.pre, dict(s, **fake)), "the registry cannot be edited around"
Path(repo, "DECISIONS.md").write_text(DEC + 'lifted: "add a rule against em dashes"\n')
assert "standing decision D1" in call(finish.pre, dict(s, **add)), "a lift quote he never said does not count"
finish.on_prompt(dict(s, prompt="ok yes, add a rule against em dashes to his prompt"))
assert call(finish.pre, dict(s, **add)) == "", "his own words this task lift it"
Path(repo, "prompt.txt").write_text("Be brief.\nBe kind.\nNever use em dashes.\n")
goal.write_text("# Goal: no em dashes\n- [x] rule added | proof: cat prompt.txt | result: there\n" + APPROACH)
stub(True)
assert "lifted:" in call(finish.stop, s), "a lifted line left behind blocks the stop"
Path(repo, "DECISIONS.md").write_text(DEC)

# ---- 3. failure triage: an added instruction needs the instruction layer named ---------------------------------
out = call(finish.stop, s)
assert "instruction-layer" in out, out
goal.write_text(goal.read_text() + "layer: instruction - he asked for the rule itself\n")
assert call(finish.stop, s) == "" and goal.with_suffix(".done.md").exists(), "named layer, reviewer ok: done"
assert reviews[-1]["tier"] == "big", "a guarded file is big work"

# ---- 4. slop: stubs and swallowed errors block; suspects go to the reviewer ---------------------------------------
repo = make_repo()
s = {"session_id": "slop", "cwd": repo, "transcript_path": "", "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="load the file"))
Path(repo, "a.py").write_text("def load():\n    try:\n        return open('f').read()\n    except OSError:\n        pass\n")
out = call(finish.stop, s)
assert "slop" in out and "swallows" in out, out
Path(repo, "a.py").write_text("def load():\n    raise NotImplementedError\n")
assert "not-implemented" in call(finish.stop, s)
Path(repo, "test_a.py").write_text("# TODO more cases\n")
Path(repo, "a.py").write_text("def load():\n    return MOCK_ROWS  # mock data\n")
stub(True)
assert call(finish.stop, s) == "", "a test file may say TODO; a suspect is the reviewer's call"
assert any("mock" in x for x in reviews[-1]["suspects"]), reviews[-1]

# ---- 5. checks: exit codes run before any reviewer and block on their own ----------------------------------------
repo = make_repo({"checks": [{"name": "compiles", "when": ["*.py"], "run": "python3 -m py_compile {files}"},
                             {"name": "no keys", "when": ["*.py"], "run": "test -z \"$ANTHROPIC_API_KEY\""}]})
os.environ["ANTHROPIC_API_KEY"] = "sk-test-should-not-reach-a-check"
s = {"session_id": "chk", "cwd": repo, "transcript_path": "", "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="tidy a.py"))
Path(repo, "a.py").write_text("x = (\n")
n = len(reviews)
out = call(finish.stop, s)
assert "checks failed" in out and "compiles" in out and "no keys" not in out, out
assert len(reviews) == n, "a failed check spends no review"
Path(repo, "a.py").write_text("x = 3\n")
assert call(finish.stop, s) == "", "checks pass, model keys stripped from their env"

# ---- 6. the loop cap: the third rejection stops it for the person ------------------------------------------------
tapped = Path(tempfile.mkdtemp(), "tapped")
repo = make_repo({"escalate": f"echo {{reason}} > {tapped}"})
s = {"session_id": "cap", "cwd": repo, "transcript_path": "", "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="make x 5"))
stub(False, "special case keyed on 5")
for i in range(2):
    Path(repo, "a.py").write_text(f"x = {5 + i}\n")
    assert "sent it back" in call(finish.stop, s)
Path(repo, "a.py").write_text("x = 9\n")
out = json.loads(call(finish.stop, s))
assert out["decision"] == "block" and "stop working on this" in out["reason"] and "systemMessage" in out, out
assert "special case" in tapped.read_text(), "the person is tapped"
assert call(finish.stop, s) == "", "after telling the person, it may stop"
finish.on_prompt(dict(s, prompt="ok, do it properly"))
assert finish.load_state(finish.paths("cap")[1]).get("review_fails") == 0, "a new task starts the count again"

# ---- 7. budget: past the task's budget Claude and the person are told once; a new task in a big session too --------
repo = make_repo({"task_budget_tokens": 1000})
tx = Path(tempfile.mkdtemp(), "t.jsonl")
tx.write_text(json.dumps({"type": "user", "message": {"content": "old task"}}) + "\n"
              + json.dumps({"type": "assistant", "message": {"id": "m0", "model": "x", "usage": {"input_tokens": 99999}}}) + "\n")
s = {"session_id": "bud", "cwd": repo, "transcript_path": str(tx), "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="do a thing"))
assert call(finish.post, s) == "", "under budget: silent"
with tx.open("a") as f:
    for i in range(3):
        f.write(json.dumps({"type": "assistant", "message": {"id": f"m{i + 1}", "model": "x",
                            "usage": {"input_tokens": 100, "output_tokens": 60, "cache_read_input_tokens": 200_000}}}) + "\n")
out = json.loads(call(finish.post, s))
assert "budget" in out["hookSpecificOutput"]["additionalContext"] and "systemMessage" in out, out
assert call(finish.post, s) == "", "told once"
st = finish.load_state(finish.paths("bud")[1]); st["tasks"] = 1; finish.save_state(finish.paths("bud")[1], st)
out = call(finish.on_prompt, dict(s, prompt="now something else"))
assert "fresh session" in out and "200k" in out, out

# ---- 9. the architecture check: the approach passes before the first edit ----------------------------------------
G = [{"name": "Prompting guide", "when": ["prompt.txt"], "version": "echo model-x", "look": "emphatic rules",
      "read": ["https://platform.claude.com/docs/en/prompting.md"]},
     {"name": "Web framework", "when": ["api/*.py"], "read": ["https://fastapi.tiangolo.com/"]}]
repo = make_repo({"grounding": G})
s = {"session_id": "arch", "cwd": repo, "transcript_path": "", "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="make bram stop ending on questions"))
goal, _ = finish.paths("arch")
goal.write_text("# Goal: no questions\n- [ ] done | proof: cat prompt.txt\n")
edit = {"tool_name": "Edit", "tool_input": {"file_path": str(Path(repo, "a.py")), "old_string": "x = 1", "new_string": "x = 2"}}
out = call(finish.pre, dict(s, **edit))
assert "## Approach" in out and "`extends:`" in out and '"deny"' in out, out
goal.write_text(goal.read_text() + APPROACH + "It edits prompt.txt.\n")
arch(False, "a second copy of the loop in a.py", cites=("a.py:1",))
n = len(archs)
out = call(finish.pre, dict(s, **edit))
assert "sent the approach back" in out and "a.py:1" in out, out
assert archs[-1]["guides"] and archs[-1]["guides"][0]["name"] == "Prompting guide", "the guide is picked from the files the plan names"
assert archs[-1]["guides"][0]["version_now"] == "model-x", "the version is read from the repo now"
call(finish.pre, dict(s, **edit))
assert len(archs) == n + 1, "the same approach is not checked twice"
goal.write_text(goal.read_text().replace("change it in place", "change it in place, keeping the loop"))
arch(False, "vibes", cites=("nowhere/else.py:3", "https://evil.example.com/x \"a sentence that is long enough\""))
assert call(finish.pre, dict(s, **edit)) == "", "an objection with no citation that holds is thrown out"
assert "thrown out" in goal.read_text()
assert finish.valid_cites(['https://platform.claude.com/docs/en/prompting.md "Tell Claude what to do instead"'],
                          repo, ["platform.claude.com"], []), "a quoted line on the guide's own site holds"
assert not finish.valid_cites(["https://platform.claude.com/x no quote here"], repo, ["platform.claude.com"], [])
assert not finish.valid_cites(["a.py:99"], repo, [], []), "a line past the end of the file does not hold"

# the third rejection (one came above) stops it for the person
for i in range(2):
    goal.write_text(goal.read_text().replace("keeping the loop", f"keeping the loop {i}"))
    arch(False, f"against the guide {i}", cites=('https://platform.claude.com/docs/en/prompting.md "Tell Claude what to do instead of what not to do"',))
    out = call(finish.pre, dict(s, **edit))
assert "stop working on this now" in out and '"deny"' in out, out
assert call(finish.stop, s) == "", "after stopping for the person, the stop passes"

# ---- 10. edits made around the gate meet the architecture check at the stop ----------------------------------------
repo = make_repo({"grounding": G})
s = {"session_id": "around", "cwd": repo, "transcript_path": "", "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="tidy"))
goal, _ = finish.paths("around")
goal.write_text("# Goal: tidy\n- [x] tidy | proof: cat a.py | result: x = 4\n" + APPROACH)
Path(repo, "a.py").write_text("x = 4\n")        # written by a shell command, not an Edit
arch(False, "a second copy", cites=("a.py:1",))
assert "sent the approach back" in call(finish.stop, s)
arch(True)
call(finish.stop, s)
assert len([a for a in archs]) and "sent the approach back" in call(finish.stop, s), "the verdict holds until the approach changes"
goal.write_text(goal.read_text().replace("change it in place", "change it in place, one line"))
stub(False, "looks off", cites=())
assert call(finish.stop, s) == "" and "review objection thrown out" in goal.with_suffix(".done.md").read_text(), \
    "an end-review objection with no citation is thrown out"

# ---- 11. the end review is handed the guide for what the code touches ---------------------------------------------
repo = make_repo({"grounding": G})
s = {"session_id": "guide", "cwd": repo, "transcript_path": "", "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="add a route"))
Path(repo, "api").mkdir(); Path(repo, "api/r.py").write_text("def r():\n    return 1\n")
stub(True)
assert call(finish.stop, s) == "" and [g["name"] for g in reviews[-1]["guides"]] == ["Web framework"], reviews[-1]
assert finish.doc_hosts(reviews[-1]["guides"]) == ["fastapi.tiangolo.com"], "the reviewer may fetch only that site"

# ---- 12. a big build in a long session goes to one fresh helper ----------------------------------------------------
repo = make_repo()
tx = Path(tempfile.mkdtemp(), "t.jsonl")
tx.write_text(json.dumps({"type": "assistant", "message": {"id": "m", "model": "x", "usage": {"cache_read_input_tokens": 200_000}}}) + "\n")
s = {"session_id": "fresh", "cwd": repo, "transcript_path": str(tx), "last_assistant_message": "done"}
finish.on_prompt(dict(s, prompt="build the thing"))
goal, _ = finish.paths("fresh")
goal.write_text("# Goal: the thing\n- [ ] it works | proof: run it\n" + APPROACH)
arch(True)
edit = {"tool_name": "Write", "tool_input": {"file_path": str(Path(repo, "t.py")), "content": "t = 1\n"}}
out = call(finish.pre, dict(s, **edit))
assert "fresh context" in out and "200k" in out and str(goal) in out, out
assert call(finish.pre, dict(s, agent_id="helper-1", agent_type="general-purpose", **edit)) == "", "the helper's edits go through"

# ---- 13. a pass that read none of the guide is run once more, told which guide it skipped -------------------------
tails = []
def lazy(tail):
    tails.append(tail)
    return ({"ok": True, "reason": "", "cites": [], "read": ["a.py"]} if len(tails) == 1 else
            {"ok": True, "reason": "", "cites": [], "read": ["https://platform.claude.com/docs/en/prompting.md"]}), ""
g = [dict(G[0], files=["prompt.txt"], version_now="")]
v, _ = finish.grounded(lazy, g)
assert len(tails) == 2 and "WITHOUT READING" in tails[1] and "Prompting guide" in tails[1] and not v.get("ungrounded")
assert finish.unread(["/x/research/y.md"], [dict(G[0], read=["~/research"])]) , "a local guide counts only under its own path"
tails.clear(); v, _ = finish.grounded(lambda t: (tails.append(t), ({"ok": True, "cites": [], "read": []}, ""))[1], g)
assert v.get("ungrounded") and len(tails) == 2, "still ungrounded after the second run: marked, not looped"

# ---- 8. the reviewer's and checks' own sessions are never gated ---------------------------------------------------
r = subprocess.run([sys.executable, finish.__file__, "stop"], input="{}", text=True, capture_output=True,
                   env=dict(os.environ, FINISH_CHECK="1"))
assert r.returncode == 0 and r.stdout == ""

# ---- 9. every reviewer run's tokens and cost are written down ---------------------------------------------------
class Done:
    stdout = json.dumps({"result": '{"ok": true, "reason": ""}', "total_cost_usd": 0.42, "num_turns": 3,
                         "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 3000,
                                   "output_tokens": 40},
                         "modelUsage": {"claude-opus-5-5": {"inputTokens": 10, "outputTokens": 40, "costUSD": 0.42,
                                                            "webSearchRequests": 0}}})
real_run = finish.subprocess.run
finish.SESSION = "s9"
finish.subprocess.run = lambda *a, **k: Done
v, err = finish.run_reviewer("p", "opus", repo, [], [], 5, "review")
def timeout(*a, **k):
    raise subprocess.TimeoutExpired("claude", 5)
finish.subprocess.run = timeout
v2, err2 = finish.run_reviewer("p", "opus", repo, [], [], 5, "arch")
finish.subprocess.run = real_run
rows = [json.loads(l) for l in finish.REVIEWS.read_text().splitlines()]
assert v["ok"] and rows[-2]["session"] == "s9" and rows[-2]["kind"] == "review" and rows[-2]["cost_usd"] == 0.42
assert rows[-2]["cache_read_input_tokens"] == 3000 and rows[-2]["by_model"]["claude-opus-5-5"] == {
    "inputTokens": 10, "outputTokens": 40, "costUSD": 0.42}
assert v2 is None and rows[-1]["kind"] == "arch" and rows[-1]["error"] == "TimeoutExpired", "a run that died is logged too"
print("ok")
