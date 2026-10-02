"""The two questions the bench never guesses: which model is this (the run
label) and is an agent chat (a harness) sharing the model server. Also the
run folder naming that makes the two answers two comparable rows."""

import datetime as dt
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests

from . import state

SCEN_SUFFIX = {"codegen": "", "exec": "-exec", "top": "-top",
               "fast": "-fast", "long": "-long"}
HARNESS_SUFFIX = {"yes": "-harness", "no": "-noharness"}
HARNESS_ADDR_FILE = os.path.join("bench", "harness-address.json")
HARNESS_ANSWER_WORDS = {"1", "2", "3", "y", "n", "yes", "no", "u", "unknown"}


def slugify(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "model"


def harness_env_hint():
    """Only ever a HINT, never an assumption: agent toolkits whose environment
    happens to be visible in this shell."""
    hints = []
    if os.environ.get("DSH_SESSION_ID") or os.environ.get("DSH_SHELL"):
        hints.append("deepseek-harness")
    if os.environ.get("CURSOR_TRACE_ID") or os.environ.get("CURSOR_SESSION_ID"):
        hints.append("cursor")
    if os.environ.get("CODEX_SHELL") or os.environ.get("CODEX_SANDBOX"):
        hints.append("codex-cli")
    if os.environ.get("CLAUDE_CODE_SESSION") or os.environ.get("CLAUDE_SESSION_ID"):
        hints.append("claude-code")
    return hints


def harness_url_hint():
    """An address for the harness, if this shell knows one (DSH_WEB_URL). It is a
    suggestion for the note only - it never decides yes/no on its own."""
    url = os.environ.get("DSH_WEB_URL") or ""
    return url.strip().rstrip("/") or None


def harness_target(note, quiet=False):
    """Turn a harness note into something reachable, or None if it is a plain name
    or a file path. Only loopback/private hosts are ever probed."""
    txt = (note or "").strip().strip('"')
    if not txt:
        return None
    if "\\" in txt or (len(txt) > 1 and txt[1] == ":" and txt[0].isalpha()):
        return None                      # a Windows path is not an address
    if "://" not in txt:
        if "." not in txt and ":" not in txt:
            return None
        txt = "http://" + txt
    try:
        parsed = urlparse(txt)
    except Exception:
        return None
    if not parsed.netloc:
        return None
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    if not (host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
            or host.startswith("127.") or host.startswith("192.168.")
            or host.startswith("10.") or host.startswith("172.")
            or host.endswith(".local") or host.endswith(".localhost")):
        if not quiet:
            print(f"     note '{note}' looks like a remote host - not probing it,"
                  " only your own machine is.")
        return None
    return f"{parsed.scheme or 'http'}://{parsed.netloc}"


def probe_harness(note, timeout=3):
    """Evidence, not a claim: can this machine reach the harness address right now?
    Never decides the harness column - only records whether the address answers."""
    target = harness_target(note)
    if not target:
        return None
    rec = {"target": target, "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        resp = requests.get(target, timeout=timeout, allow_redirects=False)
        rec.update(reachable=True, status=resp.status_code)
    except Exception as exc:
        rec.update(reachable=False, status=None, error=type(exc).__name__)
    return rec


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def harness_addr_candidates(limit=3):
    """Addresses worth offering as a choice, best first: the agent that started this
    shell, the address saved by an earlier run, and addresses recorded by recent
    harness=yes runs. Only your own machine is ever probed."""
    out = []

    def add(raw, label, probe=True):
        if len(out) >= limit:
            return
        target = harness_target(raw, quiet=True)
        if not target or any(target == got[0] for got in out):
            return
        if not probe:
            out.append((target, label))
            return
        rec = probe_harness(target) or {}
        tag = (f"answers now (HTTP {rec.get('status')})" if rec.get("reachable")
               else "not answering")
        out.append((target, f"{label} - {tag}"))

    env = harness_url_hint()
    if env:
        add(env, "the agent that started this shell (DSH_WEB_URL)")
    saved = _load_json(HARNESS_ADDR_FILE) or {}
    if saved.get("address"):
        when = ((saved.get("last_check") or {}).get("checked_at")
                or saved.get("saved_at") or "")[:10]
        add(saved["address"], f"your saved harness address (saved {when})")
    found = []
    if os.path.isdir("bench"):
        for name in os.listdir("bench"):
            path = os.path.join("bench", name, "_summary.json")
            try:
                if os.path.isfile(path):
                    found.append((os.path.getmtime(path), path))
            except OSError:
                continue
    for _, path in sorted(found, reverse=True)[:limit]:
        data = _load_json(path) or {}
        if data.get("harness") == "yes" and data.get("harness_note"):
            folder = os.path.basename(os.path.dirname(path))
            add(data["harness_note"], f"recorded by run {data.get('name') or folder}")
    if not out:
        # Nothing known: ask the usual local port once. It only becomes a choice if
        # something actually answers there.
        probe = probe_harness("http://127.0.0.1:3080")
        if probe and probe.get("reachable"):
            out.append((probe["target"], "found by asking 127.0.0.1:3080"
                                         f" (HTTP {probe.get('status')})"))
    return out[:limit]


def save_harness_addr(note, probe_rec=None):
    """Remember the harness address in use, so the next run offers it."""
    target = harness_target(note)
    if not target:
        return
    data = {"address": target,
            "saved_at": dt.datetime.now().isoformat(timespec="seconds")}
    if probe_rec:
        data["last_check"] = {k: probe_rec.get(k)
                              for k in ("reachable", "status", "checked_at")}
    try:
        os.makedirs("bench", exist_ok=True)
        with open(HARNESS_ADDR_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


def harness_auto_note():
    """The address for a run that cannot be asked (--harness yes, detached): the
    best candidate there is, or (None, None)."""
    cands = harness_addr_candidates(limit=1)
    return cands[0] if cands else (None, None)


def _ask(prompt):
    """input() that survives a closed stdin (piped runs, Start-Process, agents) and
    a Ctrl-C (no traceback - it says what to do instead)."""
    try:
        return input(prompt)
    except EOFError:
        print("\n(no keyboard here to answer with - rerun with the answer in the"
              " command line: --harness yes|no, and --name LABEL if you want a label)")
        return None
    except KeyboardInterrupt:
        raise SystemExit("\nInterrupted at the question, so nothing was started. Rerun"
                         " and answer it, or put the answers in the command line:"
                         " --harness yes|no (and --name LABEL).")


def ask_label(derived):
    """Ask what this run should be called. Refuses answers that clearly belong to
    the harness question below it - a run called "1" is not a name."""
    while True:
        got = _ask("\nQ1 - name question. What should this run be called in bench/"
                   " and in the report?\n"
                   f"     the model id is: {derived}\n"
                   "     Enter = use that; or type a shorter name (NOT 1/2/3, those"
                   " are the next question): ")
        if got is None:
            return derived
        got = got.strip()
        if not got:
            return derived
        if got.lower() in HARNESS_ANSWER_WORDS:
            print(f"     '{got}' is an answer to the harness question, which comes"
                  " after this one. Press Enter to take the model id, or type a name.")
            continue
        slug = slugify(got)
        if not slug or slug.strip("._-") == "":
            print("     That has no letters or digits in it. Press Enter to take the"
                  " model id, or type a name.")
            continue
        return slug


def ask_harness_mode(suggested, folders=None, agent=False):
    """Ask the human how this run shares the model server. Never guessed.
    folders maps the answer to the run folder it produces.
    Returns (mode, note, source) with mode in yes/no/None."""
    opts = {"no": 1, "yes": 2, None: 3}
    dflt = str(opts.get(suggested, 3))
    print("\nQ2 - harness question. This bench never guesses it, so it must be answered:")
    if agent:
        print("  Should every item be solved through DeepSeek Harness (dsh headless, full"
              " default tools, same model server)?")
    else:
        print("  Is an agent chat (a harness) open on this same model server right now?")
    for mode, num in (("no", 1), ("yes", 2)):
        if agent:
            extra = ("the agent solves each item with its tools (shell, editor, ...)"
                     if mode == "yes" else "the raw model answers, nothing else on the server")
        else:
            extra = ("its requests queue behind the benchmark, so wall time (gen-min)"
                     " inflates, tok/s does not" if mode == "yes"
                     else "your own window, nothing else hits the model server")
        folder = f"   -> bench\\{folders[mode]}" if folders else ""
        print(f"  [{num}] {mode:<6} {extra}{folder}")
    unknown_extra = "leave that column empty" + (
        f"   -> bench\\{folders[None]}" if folders else "")
    print(f"  [3] unknown  {unknown_extra}")
    hints = harness_env_hint()
    if hints:
        print("  hint: this shell looks like it was started by"
              f" {', '.join(hints)}. That is a hint only - it is not assumed.")
    while True:
        ans = _ask(f"Your choice [1/2/3] (Enter = {dflt}): ")
        if ans is None:
            return None, None, "not answered (nothing to ask here)"
        ans = ans.strip().lower() or dflt
        if ans in ("1", "no", "n"):
            mode = "no"
            break
        if ans in ("2", "yes", "y"):
            mode = "yes"
            break
        if ans in ("3", "u", "unknown"):
            return None, None, "not answered"
        print("Please answer 1, 2 or 3.")
    note = None
    if mode == "yes" and not agent:
        note = ask_harness_which(harness_addr_candidates())
    return mode, note, "you answered"


def ask_harness_which(cands):
    """Which harness it was: numbered choices, the first one is the default so
    Enter answers it and nothing has to be typed."""
    if not cands:
        got = _ask("  Which harness is it? Its address (the http://127.0.0.1:3080"
                   " style) or a name. Enter to skip: ")
        return got.strip() if got else None
    print("  Which harness is it? The bench pings an address at the start and end")
    print("  of the run, so an address is evidence; nothing is ever routed through it.")
    for num, (addr, label) in enumerate(cands, 1):
        print(f"  [{num}] {addr:<26} {label}")
    nxt = len(cands) + 1
    print(f"  [{nxt}] another one: type its address (http://host:port) or a name")
    while True:
        ans = _ask(f"  Your choice [1-{nxt}] (Enter = 1): ")
        if ans is None:
            return None
        ans = ans.strip()
        if not ans:
            return cands[0][0]
        if ans.isdigit():
            pick = int(ans)
            if 1 <= pick <= len(cands):
                return cands[pick - 1][0]
            got = _ask("      address (http://host:port) or a name: ")
            return got.strip() if got else None
        return ans


def ask_harness_pair_note(preset=None):
    """Which harness shares the model server, asked once for a --both pair (the
    no-pass needs no answer)."""
    if preset:
        return preset
    if not sys.stdin.isatty():
        guess, label = harness_auto_note()
        if guess:
            print(f"         pass 2 takes the harness from {label}: {guess}")
        else:
            print("         no keyboard and no saved harness address: pass 2 records"
                  " its yes without an address (--harness-note names one)")
        return guess
    return ask_harness_which(harness_addr_candidates())


def recorded_harness(run_dir):
    """Harness value already recorded for an existing run dir, or None."""
    sp = os.path.join(run_dir, "_summary.json")
    if not os.path.isfile(sp):
        return None
    try:
        with open(sp, encoding="utf-8") as f:
            return json.load(f).get("harness")
    except Exception:
        return None


def resolve_run_dir(base, scen, mode):
    """Pick bench/<folder> for this run: one label per model + the scenario + the
    harness answer, so the same command answered twice gives two comparable rows.
    Legacy folders (no harness suffix) are reused when their recorded answer
    agrees with this one."""
    scen_suf = SCEN_SUFFIX[scen]
    if scen_suf and base.endswith(scen_suf):
        scen_suf = ""   # --name already carries it; do not build "...-exec-exec"
    cands = []
    if mode:
        cands.append(base + scen_suf + HARNESS_SUFFIX[mode])
    cands.append(base + scen_suf)
    for c in cands:
        d = os.path.join("bench", c)
        if not os.path.isdir(d):
            continue
        rec = recorded_harness(d)
        if mode is None or rec in (None, "unknown", mode):
            return c
    return cands[0]


def both_answers(base, scen, preset_note=None, agent=False):
    """--both: one command, two passes over the same items, two comparable rows.
    Pass 1 is the raw model (nothing else should use the model server), pass 2
    is the harness pass. Nothing is asked except a final go-ahead."""
    f1 = resolve_run_dir(base, scen, "no")
    f2 = resolve_run_dir(base, scen, "yes")
    print("\nThis runs the test twice on the same items:")
    print(f"   pass 1 - the raw model       -> bench\\{f1}")
    if agent:
        print(f"   pass 2 - with the harness    -> bench\\{f2}")
        print("            (DeepSeek Harness solves each item with its tools: shell, editor,"
              " planning...; same model server, same grader)")
        note = "dsh-headless"
    else:
        print(f"   pass 2 - agent chat shared   -> bench\\{f2}")
        print("            (the raw model answers again while your agent chat works on the"
              " same server)")
        note = ask_harness_pair_note(preset_note)
        if preset_note:
            print(f"   pass 2 uses that harness: {note}")
    print("Nothing else should use the model server while it runs.")
    if sys.stdin.isatty():
        try:
            ans = input("Start now? Enter = yes, n = cancel: ")
        except EOFError:
            print("   (no keyboard: starting right away)")
            ans = ""
        except KeyboardInterrupt:
            raise SystemExit("\nCancelled, nothing was started.")
        if ans.strip().lower() in ("n", "no", "q", "quit", "cancel"):
            raise SystemExit("Cancelled, nothing was started.")
    return [("no", None, "you answered: nothing else on the server (--both pass 1)"),
            ("yes", note, ("you answered: solved through dsh headless (--both pass 2)"
                           if agent else
                           "you answered: the agent chat shared the server"
                           " (--both pass 2)"))]


def existing_run_folders(base, kind):
    """Run folders that already hold results for this model + test (+ both modes)."""
    suffix = SCEN_SUFFIX[kind]
    stem = base if (suffix and base.endswith(suffix)) else base + suffix
    found = []
    for suf in ("-noharness", "-harness", ""):
        folder = stem + suf
        d = os.path.join("bench", folder)
        gen = os.path.join(d, "generations.jsonl")
        summ = _load_json(os.path.join(d, "_summary.json"))
        if not os.path.isfile(gen) and not summ:
            continue
        n = sum((summ or {}).get("counts", {}).values()) if summ else 0
        if not n and os.path.isfile(gen):
            try:
                with open(gen, encoding="utf-8") as f:
                    n = sum(1 for line in f if line.strip())
            except OSError:
                n = 0
        hv = (summ or {}).get("harness")
        if hv == "yes":
            mode = ("agent (dsh)" if (summ or {}).get("harness_mode") == "agent"
                    else "agent chat shared the server (old style)")
        elif hv == "no":
            mode = "raw model"
        else:
            mode = "mode not recorded"
        found.append({"folder": folder, "score": (summ or {}).get("score"), "items": n,
                      "date": ((summ or {}).get("generated_at") or "")[:10],
                      "mode": mode, "finished": bool(summ)})
    return found


def ask_existing(runs):
    """Q0: this model already has results for this test. Returns 'fresh' or
    'resume'; cancel exits."""
    print("\nQ0 - existing results. This model already has results for this test:")
    for r in runs:
        score = f"{r['score']:.1f}%" if isinstance(r["score"], (int, float)) else "no score"
        print(f"   bench\\{r['folder']}\n        {score}, {r['items']} items, {r['date'] or 'unfinished'},"
              f" {r['mode']}")
    default = "1" if all(r["finished"] for r in runs) else "2"
    print("What do you want to do?")
    print("  [1] Start fresh - the old results move to bench\\_archive (nothing is erased,"
          " --restore-run brings them back)")
    print("  [2] Resume - keep the finished items and continue where it stopped (a run"
          " that is already complete adds nothing new)")
    print("  [3] Cancel")
    while True:
        ans = _ask(f"Your choice [1/2/3] (Enter = {default}): ")
        if ans is None:
            return "resume"
        ans = ans.strip() or default
        if ans == "1":
            return "fresh"
        if ans == "2":
            return "resume"
        if ans == "3":
            raise SystemExit("Cancelled, nothing was started.")
        print("Please answer 1, 2 or 3.")


def begin_pass(run_slug, run_dir, model_id, harness_val, harness_note, harness_src,
               agent=False):
    """Everything a pass prints and checks before it touches the model: the
    run banner, the console hint and the harness address ping.
    Returns (harness_note, harness_probe, resuming)."""
    if run_slug in state.FRESH_RUNS and os.path.isdir(run_dir):
        from .console import archive_run
        dest, complaint = archive_run({"path": run_dir, "folder": run_slug})
        state.FRESH_RUNS.discard(run_slug)
        print(f"Starting fresh: the old results of bench\\{run_slug} were moved to {dest}"
              if not complaint else f"Could not archive the old results ({complaint}); resuming.")
    resuming = os.path.isfile(os.path.join(run_dir, "generations.jsonl"))
    os.makedirs(run_dir, exist_ok=True)
    if harness_val == "yes" and not harness_note and not agent:
        guess, label = harness_auto_note()
        if guess:
            harness_note = guess
            harness_src += f"; address = {label}, override with --harness-note"
    print(f"\nRun:     bench\\{run_slug}  ({'resuming' if resuming else 'new run'})")
    print(f"Model:   {model_id or 'UNKNOWN - pass --model <server model id>'}")
    print(f"Harness: {harness_val or 'unknown'} ({harness_src}"
          + (f"; {harness_note}" if harness_note else "") + ")")
    # The bench console: any key during the run opens it, so scores can be
    # listed and deleted while the model is still working. Only with a keyboard.
    state.CONSOLE_ACTIVE["run"] = run_dir
    if sys.stdin.isatty():
        state.CONSOLE_LIVE = True
        print("           console: type 'help' any time during the run (the first"
              " keypress opens\n           bench> - list / delete 2 / restore 1 /"
              " status / quit)")
    # The harness note may carry an address: check it is really there. Evidence
    # only - it can never turn a "no" into a "yes" or the other way round.
    probe = {}
    if harness_val == "yes" and agent:
        print("           harness: each item is solved by dsh headless with its full default"
              " toolset (no chat window needed)")
    elif harness_val == "yes":
        start = probe_harness(harness_note)
        save_harness_addr(harness_note, start)
        if start:
            probe["start"] = start
            print(f"           {start['target']} is"
                  + (f" up (HTTP {start['status']}) - the agent is really there"
                     if start.get("reachable") else
                     f" NOT answering ({start.get('error')}) - your answer stands"))
        else:
            print("           no address for the harness, so nothing to check: answer"
                  " the which-harness question with one (Enter takes choice 1), or"
                  " pass --harness-note http://127.0.0.1:3080")
    return harness_note, probe, resuming


def end_probe(harness_val, harness_note, probe):
    """Ping the harness address again at the end of the pass (evidence only)."""
    if harness_val != "yes":
        return
    end = probe_harness(harness_note)
    if end:
        probe["end"] = end
        print(f"End of run: {end['target']} is"
              + (f" still up (HTTP {end['status']})" if end.get("reachable") else
                 f" not answering anymore ({end.get('error')})"))


def harness_line(harness_val, harness_src, harness_note, probe):
    parts = []
    for key, where in (("start", "at start"), ("end", "at end")):
        p = probe.get(key)
        if p:
            parts.append(("up" if p.get("reachable")
                          else f"not answering ({p.get('error')})") + f" {where}")
    return (f"   harness: {harness_val or 'unknown'} ({harness_src}"
            + (f"; {harness_note}" if harness_note else "")
            + (f" - {', '.join(parts)}" if parts else "") + ")")
