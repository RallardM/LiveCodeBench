"""The harness pass for real: every item is solved by DeepSeek Harness (dsh) in
its headless profile, with the default complete toolset (shell, editor, file
tools, planning, sub-agents, skills, context compaction), talking to the same
llama-server the bench measures. The agent's final message goes through the
same extractor and grader as a raw answer, so both rows are directly comparable.

How it is driven (verified against the installed dsh, headless profile):
  dsh --profile headless --patch <bench patch> --json -      (task on stdin)
  * the patch defines a provider that points at --base-url with the server's
    model id, makes it the default model and (by default) switches the web
    tools off so the agent cannot look the benchmark answers up;
  * DSH_PERMISSION_MODE=danger-full-access lets the agent run code without an
    approval prompt (nobody can answer prompts in headless mode);
  * every item runs in its own empty temporary directory;
  * the --json events give steps, tokens, tool calls and the final message."""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zlib

import requests

from . import state
from .generate import auth_headers, generate_one

DEFAULT_HOME = os.path.join("bench", "_dsh_home")
PROVIDER = "lcb-local"
API_KEY_ENV = "LCB_DSH_API_KEY"
WEB_TOOLS = ("web_search", "web_fetch")
SUSPECT_WORDS = ("huggingface", ".cache", "eval_results", "generations.jsonl",
                 "sample_ids", "lcb_bench", "lcbbench", "_dsh_home", "hf_token",
                 "load_dataset")
NO_CAP = 10 ** 9

AGENT_ADDENDUM = (
    "\n\n---\nThis is an evaluation. You are in an empty working directory and may "
    "use your tools freely: run Python or shell commands, write and test files, "
    "check calculations. Solve the task yourself: do not search the internet and "
    "do not read files outside this directory. When you are finished, your final "
    "message must contain the answer in exactly the format the task asks for - "
    "only that final message is graded."
)

INSTALL_HELP = (
    "DeepSeek Harness (dsh) was not found. Install it once, then rerun:\n"
    "    npm install -g @deepseek-ai/dsh          (needs Node.js 22 or newer)\n"
    "or point the bench at it: --harness-cmd \"C:\\path\\to\\dsh.cmd\" (or set"
    " DSH_CMD).\nTo keep the old behaviour (the raw model answers, the harness"
    " only shares the server) use --harness-mode raw."
)


def resolve_dsh_cmd(explicit=None):
    """The command list that starts dsh, or None. Order: --harness-cmd, DSH_CMD,
    `dsh` on PATH, then `npx --yes @deepseek-ai/dsh`."""
    cand = explicit or os.environ.get("DSH_CMD")
    if cand:
        parts = shlex.split(cand, posix=(os.name != "nt"))
        return [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p
                for p in parts]
    found = shutil.which("dsh")
    if found:
        return [found]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "@deepseek-ai/dsh"]
    return None


def server_context_window(base_url):
    """n_ctx of the llama.cpp server behind base_url (its /props), or None."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        data = requests.get(root + "/props", timeout=5, headers=auth_headers()).json()
        n = (data.get("default_generation_settings") or {}).get("n_ctx")
        return int(n) if n else None
    except Exception:
        return None


def yaml_str(value):
    """A YAML-safe scalar (JSON strings are valid YAML)."""
    return json.dumps(str(value))


def write_patch(path, base_url, model_id, cap, ctx, web=False):
    """The bench overlay for dsh: one provider at the bench's own server, made the
    default model, session titles off (they cost an extra model call), web tools
    off unless asked for."""
    lines = [
        "- id: llm-pi-ai",
        "  config:",
        "    providers:",
        f"      {PROVIDER}:",
        "        displayName: Bench local server",
        f"        apiKeyEnv: {API_KEY_ENV}",
        "        api: openai-completions",
        f"        baseURL: {yaml_str(base_url)}",
        f"        defaultContextWindow: {int(ctx)}",
        f"        defaultMaxTokens: {int(cap)}",
        "        compat:",
        "          maxTokensField: max_tokens",
        "          supportsDeveloperRole: false",
        "        models:",
        f"          - id: {yaml_str(model_id)}",
        "- id: agent-default-model",
        "  config:",
        f"    provider: {PROVIDER}",
        f"    model: {yaml_str(model_id)}",
        "- id: session-title-llm",
        "  disabled: true",
    ]
    if not web:
        lines += ["- id: tool-web", "  disabled: true"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def child_env(args):
    """The environment of a dsh child: its own home, the bench server's key, the
    permission mode, telemetry off, and none of the nested-agent markers."""
    env = dict(os.environ)
    for key in ("DSH_SESSION_ID", "DSH_SHELL", "DSH_PROFILE", "DSH_WEB_URL"):
        env.pop(key, None)
    home = os.path.abspath(os.path.expanduser(args.harness_home or DEFAULT_HOME))
    os.makedirs(home, exist_ok=True)
    env["DSH_HOME"] = home
    env[API_KEY_ENV] = state.API_KEY or "none"
    env["DSH_PERMISSION_MODE"] = ("danger-full-access" if args.harness_access == "full"
                                  else "workspace-write")
    env["DSH_TELEMETRY_DISABLED"] = "1"
    return env


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def parse_events(text):
    """The run events of `dsh --json` (one JSON object per line) folded into one
    dict: session id, steps, token sums, tool calls, the final message and how
    the turn ended."""
    ev = {"session_id": None, "steps": 0, "in_tok": 0, "out_tok": 0, "reason": None,
          "error": None, "texts": [], "final": None, "calls": [], "tool_ok": 0,
          "tool_err": 0, "bad_lines": 0}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            ev["bad_lines"] += 1
            continue
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        if kind == "session":
            ev["session_id"] = e.get("sessionId")
        elif kind == "status":
            phase = e.get("phase")
            if phase == "step_end":
                ev["steps"] += 1
                usage = e.get("usage") or {}
                ev["in_tok"] += int(usage.get("inputTokens") or 0)
                ev["out_tok"] += int(usage.get("outputTokens") or 0)
            elif phase == "turn_end":
                reason = e.get("reason") or {}
                ev["reason"] = reason.get("kind")
                if reason.get("kind") == "error":
                    err = reason.get("error")
                    ev["error"] = (err.get("message") if isinstance(err, dict) else None) or str(err)
        elif kind == "text":
            ev["texts"].append(e.get("text") or "")
        elif kind == "tool_call":
            ev["calls"].append({"tool": e.get("tool"), "input": e.get("input")})
        elif kind == "tool_result":
            if e.get("status") == "completed":
                ev["tool_ok"] += 1
            else:
                ev["tool_err"] += 1
        elif kind == "final":
            ev["final"] = e.get("text")
        elif kind == "error":
            ev["error"] = e.get("message")
    return ev


def find_suspects(calls):
    """Tool calls that look like a lookup of the benchmark instead of solving it:
    web tools, or commands touching dataset caches and bench files."""
    hits = []
    for c in calls:
        tool = str(c.get("tool") or "")
        blob = json.dumps(c.get("input"), ensure_ascii=False).lower()
        if tool in WEB_TOOLS:
            hits.append(f"{tool}")
        else:
            for word in SUSPECT_WORDS:
                if word in blob:
                    hits.append(f"{tool}:{word}")
                    break
    return hits


def build_task(messages):
    """The bench prompt (system + user text, unchanged) plus the evaluation
    note. Same wording for every item, so the only difference to the raw pass is
    what the agent can do."""
    parts = [m.get("content") or "" for m in messages]
    return "\n\n".join(p for p in parts if p).strip() + AGENT_ADDENDUM


def run_dsh(cmd, argv_tail, task_text, cwd, env, timeout_s):
    """One dsh process. Returns (stdout, stderr, returncode, timed_out)."""
    kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
              cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace")
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(list(cmd) + list(argv_tail), **kw)
    timed_out = False
    try:
        out, err = proc.communicate(input=task_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=20)
        except Exception:
            out, err = "", ""
    return out or "", err or "", proc.returncode, timed_out


class AgentRunner:
    """Solves bench items through dsh headless; thread-safe."""

    def __init__(self, args, run_dir, model_id):
        self.cmd = resolve_dsh_cmd(args.harness_cmd)
        if not self.cmd:
            raise SystemExit(INSTALL_HELP)
        self.args = args
        self.model_id = model_id
        self.env = child_env(args)
        self.ctx = server_context_window(args.base_url) or 65536
        self.patch_dir = os.path.join(run_dir, "harness_patches")
        self.events_dir = os.path.join(run_dir, "harness_events")
        os.makedirs(self.events_dir, exist_ok=True)
        self.extra_patches = [os.path.abspath(p) for p in (args.harness_patch or [])]
        self.lock = threading.Lock()
        self.patches = {}
        self.fail_streak = 0
        self.aborted = None
        self.base_timeout_s = float(args.harness_timeout) * 60.0

    def patch_for(self, cap):
        with self.lock:
            if cap not in self.patches:
                path = os.path.abspath(os.path.join(self.patch_dir, f"cap-{cap}.yml"))
                write_patch(path, self.args.base_url, self.model_id, cap, self.ctx,
                            web=self.args.harness_web)
                self.patches[cap] = path
            return self.patches[cap]

    def _event_file(self, qid):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(qid))[:80]
        return os.path.join(self.events_dir,
                            f"{safe}-{zlib.crc32(str(qid).encode('utf-8')):08x}.jsonl")

    def generate(self, qid, si, messages, cap, deadline=None):
        """One item -> a generation record shaped like generate_one's."""
        if self.aborted:
            return None
        timeout_s = self.base_timeout_s
        if deadline is not None:
            timeout_s = min(timeout_s, max(300.0, deadline - time.time() + 120.0))
        tail = ["--profile", "headless", "--patch", self.patch_for(cap)]
        for extra in self.extra_patches:
            tail += ["--patch", extra]
        tail += ["--json", "-"]
        work = tempfile.mkdtemp(prefix="lcb-dsh-")
        t0 = time.time()
        try:
            out, err, code, timed_out = run_dsh(self.cmd, tail, build_task(messages),
                                                work, self.env, timeout_s)
        except Exception as exc:  # noqa: BLE001
            out, err, code, timed_out = "", f"{type(exc).__name__}: {exc}", -1, False
        finally:
            shutil.rmtree(work, ignore_errors=True)
        seconds = time.time() - t0
        ev = parse_events(out)
        try:
            with open(self._event_file(qid), "w", encoding="utf-8") as f:
                f.write(out)
        except OSError:
            pass
        final = ev["final"] if ev["final"] is not None else (
            ev["texts"][-1] if ev["texts"] else "")
        started = ev["session_id"] is not None
        failed = (not started) or ev["reason"] == "error" or (
            code not in (0, None) and ev["reason"] is None and not timed_out)
        agent = {
            "session_id": ev["session_id"], "reason": ev["reason"], "timed_out": timed_out,
            "steps": ev["steps"], "tool_calls": len(ev["calls"]),
            "tools": {}, "tool_errors": ev["tool_err"],
            "suspect": find_suspects(ev["calls"]), "exit_code": code,
        }
        for c in ev["calls"]:
            name = str(c.get("tool"))
            agent["tools"][name] = agent["tools"].get(name, 0) + 1
        rec = {"qid": qid, "si": si, "output": final, "seconds": round(seconds, 2),
               "completion_tokens": ev["out_tok"] or None,
               "prompt_tokens": ev["in_tok"] or None, "max_tokens": NO_CAP,
               "timings": None, "failed": False, "agent": agent}
        if failed:
            why = ev["error"] or (err.strip()[-400:] if err.strip() else f"exit code {code}")
            rec.update(failed=True, error=str(why)[:500])
        with self.lock:
            self.fail_streak = self.fail_streak + 1 if failed else 0
            if self.fail_streak >= 3 and not self.aborted:
                self.aborted = (f"3 items in a row failed to run through dsh (last: "
                                f"{str(rec.get('error'))[:300]}). Run `python lcb_bench.py "
                                "--harness-check` to see why, or use --harness-mode raw.")
        return rec


def agent_summary(done, runner=None, version=None, args=None):
    """Totals over every agent record of a run, for the summary and the row."""
    recs = [r["agent"] for r in done.values() if r.get("agent")]
    if not recs:
        return None
    tools = {}
    for a in recs:
        for k, v in (a.get("tools") or {}).items():
            tools[k] = tools.get(k, 0) + v
    out = {
        "tasks": len(recs), "steps": sum(a["steps"] for a in recs),
        "tool_calls": sum(a["tool_calls"] for a in recs), "tools": tools,
        "tool_errors": sum(a["tool_errors"] for a in recs),
        "timeouts": sum(1 for a in recs if a["timed_out"]),
        "items_with_suspect_calls": sum(1 for a in recs if a["suspect"]),
        "input_tokens": sum((r.get("prompt_tokens") or 0) for r in done.values() if r.get("agent")),
        "output_tokens": sum((r.get("completion_tokens") or 0) for r in done.values() if r.get("agent")),
        "no_tool_items": sum(1 for a in recs if not a["tool_calls"]),
        "dsh_version": version,
    }
    if args is not None:
        out.update(access=args.harness_access, web_tools=bool(args.harness_web),
                   home=os.path.abspath(os.path.expanduser(args.harness_home or DEFAULT_HOME)),
                   timeout_min=args.harness_timeout)
    return out


def print_agent_lines(summary):
    if not summary:
        return
    tools = ", ".join(f"{k} {v}" for k, v in sorted(summary["tools"].items(),
                                                     key=lambda kv: -kv[1])[:6]) or "none"
    print(f"   agent: {summary['tasks']} items through dsh {summary.get('dsh_version') or ''}"
          f" ({summary.get('access')} access), {summary['steps']} steps,"
          f" {summary['tool_calls']} tool calls ({tools}), {summary['tool_errors']} tool errors,"
          f" {summary['timeouts']} timeouts")
    if summary["no_tool_items"] == summary["tasks"]:
        print("      the agent never used a tool: if that is not what you expect, check that"
              " llama-server was started with --jinja (tool calling) - python lcb_bench.py"
              " --harness-check shows it")
    if summary["items_with_suspect_calls"]:
        print(f"      WARNING: {summary['items_with_suspect_calls']} item(s) used tools that"
              " look like a lookup (web or dataset/bench files). The raw events are in"
              " bench\\<run>\\harness_events\\ - review them, or rerun without --harness-web.")


def make_generator(args, run_dir, model_id, agent):
    """(gen, runner): gen(qid, si, messages, cap, deadline=None) -> record or None.
    agent=False is the raw model call, agent=True goes through dsh."""
    if agent:
        runner = AgentRunner(args, run_dir, model_id)
        return runner.generate, runner
    base_extras = {"temperature": args.temperature, "top_p": args.top_p}
    if args.extra_body:
        base_extras.update(json.loads(args.extra_body))

    def gen(qid, si, messages, cap, deadline=None):
        return generate_one((qid, si, messages), args.base_url, model_id,
                            dict(base_extras, max_tokens=cap), 3, args.request_timeout)
    return gen, None


def preflight(args, model_id, smoke=True):
    """Before any long run: is dsh there, does the bench overlay boot, and (smoke)
    can the agent really answer through this server and use a tool. Returns
    {'version': str}. Stops with a clear message when something is broken."""
    cmd = resolve_dsh_cmd(args.harness_cmd)
    if not cmd:
        raise SystemExit(INSTALL_HELP)
    env = child_env(args)
    print("\nHarness check: DeepSeek Harness (dsh) headless, full default toolset")
    try:
        ver = subprocess.run(list(cmd) + ["--version"], capture_output=True, text=True,
                             env=env, timeout=300, encoding="utf-8", errors="replace")
        version = (ver.stdout or "").strip().splitlines()[-1] if ver.stdout.strip() else "unknown"
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Could not start dsh ({type(exc).__name__}: {exc}).\n" + INSTALL_HELP)
    print(f"   dsh: {' '.join(cmd)}   version {version}")
    ctx = server_context_window(args.base_url)
    work_root = os.path.join("bench", "_harness_check")
    patch = os.path.abspath(write_patch(os.path.join(work_root, "check.yml"),
                                        args.base_url, model_id, 4096, ctx or 65536,
                                        web=args.harness_web))
    try:
        dump = subprocess.run(list(cmd) + ["--profile", "headless", "--patch", patch,
                                           "--dump-config"], capture_output=True, text=True,
                              env=env, timeout=300, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"dsh could not compose its headless profile ({exc}).")
    if dump.returncode != 0 or PROVIDER not in (dump.stdout or ""):
        raise SystemExit("dsh did not accept the bench overlay:\n" + (dump.stderr or dump.stdout)[-800:])
    print(f"   overlay: provider {PROVIDER} -> {args.base_url}, model {model_id},"
          f" context {ctx or '65536 (server did not say)'}, web tools"
          f" {'ON' if args.harness_web else 'off'}, access {args.harness_access}")
    info = {"version": version}
    if not smoke:
        return info
    task = ("Use your shell tool to run this command: python -c \"print(6*7)\" (use python3 "
            "if python is not found). Then finish with exactly this last line: Answer: 42")
    work = tempfile.mkdtemp(prefix="lcb-dsh-check-")
    t0 = time.time()
    try:
        out, err, code, timed_out = run_dsh(cmd, ["--profile", "headless", "--patch", patch,
                                                  "--json", "-"], task, work, env, 600)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ev = parse_events(out)
    final = ev["final"] if ev["final"] is not None else (ev["texts"][-1] if ev["texts"] else "")
    secs = time.time() - t0
    if ev["session_id"] is None or ev["reason"] == "error" or timed_out:
        why = "timed out after 10 min" if timed_out else (ev["error"] or err.strip()[-600:] or f"exit code {code}")
        raise SystemExit(f"Smoke task failed ({why}).\nIs the model server up at {args.base_url}?"
                         " Is the model id right? The agent needs tool calling, so start"
                         " llama-server with --jinja.")
    print(f"   smoke task: {ev['steps']} step(s), {len(ev['calls'])} tool call(s)"
          f" ({ev['tool_ok']} ok, {ev['tool_err']} error), {secs:.0f}s, answer"
          f" {'42 (correct)' if '42' in final else repr(final[-60:])}")
    if not ev["calls"]:
        print("   WARNING: the model answered without calling a tool. The agent pass still"
              " works but then it is the model alone in an agent prompt. For tool calling"
              " start llama-server with --jinja (and a chat template with tool support).")
    elif ev["tool_err"] and not ev["tool_ok"]:
        print("   WARNING: every tool call failed. On Windows the shell tool needs PowerShell 7"
              " (pwsh on PATH); with --harness-access workspace the Windows sandbox can also"
              " fail - keep the default --harness-access full.")
    info["smoke"] = {"steps": ev["steps"], "tool_calls": len(ev["calls"]),
                     "tool_errors": ev["tool_err"], "answer_ok": "42" in final}
    return info
