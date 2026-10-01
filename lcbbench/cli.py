"""Command line: argument parsing and the top-level flow (who is being
benched, in which harness mode, which pipeline runs)."""

import argparse
import os
import sys
import time

import requests

from . import state
from .advice import show_pool_info
from .console import (archive_run, console_entries, console_find, console_list,
                      console_loop, restore_run)
from .generate import auth_headers
from .harness import (HARNESS_SUFFIX, SCEN_SUFFIX, ask_harness_mode, ask_label,
                      both_answers, probe_harness, recorded_harness,
                      save_harness_addr, slugify)
from .pipeline import run_one
from .report import build_report
from .sampling import parse_mix
from .selftest import self_test

USAGE = """\
lcb_bench.py - fast, resumable, Windows-friendly benchmark for local
OpenAI-compatible model servers (llama.cpp, LM Studio, vLLM, Ollama...).

The two commands you need (from your own PowerShell window):
  python lcb_bench.py --suite fast --both    # all skills, easy to hardest, 2 h
  python lcb_bench.py --suite long --both    # code generation, 5 h
  python lcb_bench.py --report-only          # every run side by side

Two things are asked out loud and never guessed:
  WHICH MODEL: the label defaults to the model id the server reports.
  WHICH MODE:  is an agent chat (a harness) sharing the model server? The answer
               becomes part of the run folder (-harness / -noharness), so both
               modes are two comparable rows. --both runs both passes itself.

Everything checkpoints under bench/<run>/, so after a crash or a closed window
you rerun the same command and it resumes.
"""


def build_parser():
    ap = argparse.ArgumentParser(
        description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--suite", choices=["fast", "long"], default=None,
                    help="The one-command tests. fast = code, math, science and"
                         " code reading, four tiers each (easy to hardest), short"
                         " answer caps, 120 min in total. long = code generation,"
                         " four tiers, full answer cap, 300 min in total. Both are"
                         " time-boxed: what finishes inside the budget is scored.")
    ap.add_argument("--budget-min", type=int, default=None, metavar="MIN",
                    help="Total minutes of generation for the whole command (both"
                         " passes with --both). Default: 120 fast, 300 long.")
    ap.add_argument("--skills", default=None, metavar="LIST",
                    help="Suite only: comma list from code,math,science,reading."
                         " Default: all four for fast, code for long.")
    ap.add_argument("--rounds", type=int, default=0, metavar="N",
                    help="Suite only: stop after N full rounds (one item of every"
                         " skill x tier cell per round) even if time is left. Use"
                         " it to make two models finish exactly the same items.")
    ap.add_argument("--apex", action="store_true",
                    help="Suite only: the hardest math tier becomes MathArena Apex"
                         " (problems most frontier models fail) instead of AIME 2026"
                         " and HMMT Feb 2026. For strong models.")
    ap.add_argument("--name", help="Short label for this model (folder + report row)."
                                   " Optional: without it the label comes from the model"
                                   " id the server reports.")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1",
                    help="OpenAI-compatible base URL (LM Studio: http://localhost:1234/v1)")
    ap.add_argument("--api-key", default=None,
                    help="Bearer token for servers that demand one (vLLM and friends)."
                         " Also read from LCB_API_KEY. Not needed for llama-server.")
    ap.add_argument("--model", default=None, help="Server model id (default: first from /v1/models)")
    ap.add_argument("--n", type=int, default=1, help="Samples per problem")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--max-tokens", type=int, default=16384,
                    help="Answer cap. In --suite fast the tiers use shorter caps"
                         " (3072 / 4096 / 6144 / 8192), never above this value.")
    ap.add_argument("--workers", type=int, default=1,
                    help="Concurrent requests; set = llama-server --parallel for max throughput")
    ap.add_argument("--extra-body", default=None,
                    help='Extra JSON merged into every request, e.g. '
                         '\'{"chat_template_kwargs": {"enable_thinking": false}}\'')
    ap.add_argument("--request-timeout", type=float, default=3600)
    ap.add_argument("--release", default="release_latest")
    ap.add_argument("--start-date", default=None, help="YYYY-MM-DD filter on problem publish date")
    ap.add_argument("--end-date", default=None)
    ap.add_argument("--difficulty", default=None, choices=["easy", "medium", "hard"])
    ap.add_argument("--limit", type=int, default=0, help="Only first N problems (smoke test)")
    ap.add_argument("--random-sample", type=int, default=0,
                    help="Stratified random subset of N problems (same subset every"
                         " time via fixed seed, saved to bench/<name>/sample_ids.json)")
    ap.add_argument("--hardest", type=int, default=0, metavar="N",
                    help="Take N of the sample's slots for the hardest problems in"
                         " the release and spread the rest evenly over easy / medium"
                         " / hard. On its own the run IS those N hardest problems.")
    ap.add_argument("--mix", default=None, metavar="A/B/C/D",
                    help="Fill --random-sample N by tier shares: --mix 50/25/15/10 is"
                         " 50%% hardest, 25%% hard, 15%% medium, 10%% easy. Named form:"
                         " --mix hardest=50,hard=25,medium=15,easy=10. Replaces"
                         " --hardest.")
    ap.add_argument("--pool-info", action="store_true",
                    help="Print what a scenario's pool can be sampled from. No"
                         " generation, no server.")
    ap.add_argument("--bundle", type=int, default=1, metavar="K",
                    help="code_execution / test_output_prediction only: every item"
                         " is K calls asked in ONE answer, all K right or it fails.")
    ap.add_argument("--harden-target", type=float, default=None, metavar="PCT",
                    help="A single-scenario run at or over PCT%% prints the next"
                         " command that could push this model under it.")
    ap.add_argument("--both", action="store_true",
                    help="One command, both rows: run the test twice on the same"
                         " items - pass 1 with the harness question answered no"
                         " (nothing else on the model server), pass 2 answered yes"
                         " (the agent chat working while the bench runs).")
    ap.add_argument("--extractor", default="auto", choices=["auto", "official"],
                    help="auto = official last-fence rule + truncation/fenceless fallback")
    ap.add_argument("--timeout", type=int, default=6,
                    help="Eval time budget per test case, in seconds (official: 6)")
    ap.add_argument("--eval-workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--skip-generate", action="store_true")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--report-only", action="store_true",
                    help="Print the full comparison table of every run under bench/")
    ap.add_argument("--manage", action="store_true",
                    help="Open the bench console without a server: list the scores,"
                         " delete one, bring it back. Type help.")
    ap.add_argument("--list-runs", action="store_true",
                    help="Print the numbered list of runs the bench console works on")
    ap.add_argument("--delete-run", default=None, metavar="NAME_OR_NUMBER",
                    help="Move one run out of the report into bench/_archive"
                         " (reversible with --restore-run). No server needed.")
    ap.add_argument("--restore-run", default=None, metavar="NAME_OR_NUMBER",
                    help="Move a run back out of bench/_archive into bench/")
    ap.add_argument("--speed-probe", action="store_true",
                    help="Measure server prefill + decode tok/s with 2 probe requests"
                         " (suites always do)")
    ap.add_argument("--probe-only", action="store_true",
                    help="With --speed-probe: measure tok/s and exit")
    ap.add_argument("--scenario", default="code_generation",
                    choices=["code_generation", "codegen", "code_execution", "exec",
                             "test_output_prediction", "top"],
                    help="Single scenario (ignored with --suite). code_generation is"
                         " the leaderboard one; code_execution and"
                         " test_output_prediction are narrow, quick ceiling checks.")
    ap.add_argument("--exec-cot", action="store_true",
                    help="Step-by-step examples for code_execution")
    ap.add_argument("--self-test", action="store_true",
                    help="Sanity-check the grader and exit (no server needed)")
    ap.add_argument("--harness", default=None, choices=["yes", "no"],
                    help="Answer the harness question up front instead of being asked."
                         " Needed when the run is detached or piped. Never guessed.")
    ap.add_argument("--harness-note", default=None, metavar="ADDRESS_OR_NAME",
                    help="With --harness yes (or --both): say WHICH harness shared"
                         " the server. An address is pinged at start and end.")
    ap.add_argument("--set-harness", default=None, choices=["yes", "no"],
                    metavar="{yes,no}",
                    help="With --name: backfill the harness column of an existing run"
                         " and rebuild the report (no server, no regeneration)")
    return ap


def _set_harness(args, ap):
    import json

    if not args.name:
        ap.error("--set-harness needs --name of an existing run folder")
    sp = os.path.join("bench", slugify(args.name), "_summary.json")
    if not os.path.isfile(sp):
        known = sorted(d for d in os.listdir("bench")
                       if os.path.isfile(os.path.join("bench", d, "_summary.json")))
        sys.exit(f"No summary for '{args.name}' at {sp} - nothing to annotate."
                 f" Runs you have: {', '.join(known) or '(none)'}")
    with open(sp, encoding="utf-8") as f:
        data = json.load(f)
    data["harness"] = args.set_harness
    if args.harness_note:
        data["harness_note"] = args.harness_note
        now = probe_harness(args.harness_note)
        save_harness_addr(args.harness_note, now)
        if now:
            now["note"] = "checked now, after the run - not evidence about the run itself"
            data["harness_probe"] = {"backfill": now}
            print(f"     {now['target']} is"
                  + (f" up now (HTTP {now['status']})" if now.get("reachable")
                     else f" not answering ({now.get('error')})"))
        else:
            data["harness_probe"] = None
        prev_src = data.get("harness_source")
        data["harness_source"] = (prev_src + "; note set with --set-harness"
                                  if prev_src else "backfilled with --set-harness")
    else:
        data["harness_source"] = "backfilled with --set-harness"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"{slugify(args.name)}: harness = {args.set_harness}"
          + (f", note = {args.harness_note}" if args.harness_note else ""))
    build_report()


def _run_management(args):
    """--list-runs / --delete-run / --restore-run / --manage. True if handled."""
    if args.list_runs:
        console_list("runs")
        if console_entries(archived=True):
            console_list("archive")
        return True
    if args.delete_run is not None:
        rows = console_entries()
        row, complaint = console_find(args.delete_run, rows, "run")
        if complaint:
            console_list("runs")
            sys.exit(complaint)
        dest, complaint = archive_run(row)
        if complaint:
            sys.exit(complaint)
        print(f"deleted {row['label']}: moved to {dest}")
        print("nothing was erased - --restore-run (or 'restore' in the console)"
              " brings it back; bench/report.md + .csv are rebuilt without it")
        build_report(show=False)
        return True
    if args.restore_run is not None:
        rows = console_entries(archived=True)
        row, complaint = console_find(args.restore_run, rows, "archived run")
        if complaint:
            print("Archived runs:")
            console_list("archive")
            sys.exit(complaint)
        back, complaint = restore_run(row)
        if complaint:
            sys.exit(complaint)
        print(f"restored {row['label']} -> {back}, it counts in the report again")
        build_report(show=False)
        return True
    if args.manage:
        console_loop()
        return True
    return False


def main():
    ap = build_parser()
    args = ap.parse_args()
    state.API_KEY = args.api_key or os.environ.get("LCB_API_KEY")

    scen = {"code_generation": "codegen", "codegen": "codegen",
            "code_execution": "exec", "exec": "exec",
            "test_output_prediction": "top", "top": "top"}[args.scenario]

    if args.probe_only and not (args.speed_probe or args.suite):
        ap.error("--probe-only requires --speed-probe")
    if args.both and args.harness:
        ap.error("--both runs the test twice and answers the harness question"
                 " both ways; --harness answers it once. Use one or the other.")
    if args.harness_note and args.harness != "yes" and args.set_harness != "yes" \
            and not args.both:
        ap.error("--harness-note needs --harness yes (it says which harness it was)")
    if args.mix and args.hardest:
        ap.error("--mix and --hardest both decide how the sample gets its hard"
                 " problems; use one or the other")
    if args.suite and (args.mix or args.hardest or args.random_sample or args.bundle != 1):
        ap.error("--suite picks its own items: drop --mix / --hardest /"
                 " --random-sample / --bundle (they belong to --scenario runs)."
                 " --budget-min, --skills, --rounds and --start-date shape a suite.")
    if not args.suite and (args.budget_min or args.skills or args.rounds or args.apex):
        ap.error("--budget-min, --skills, --rounds and --apex belong to --suite")
    if args.bundle != 1:
        if args.bundle < 2:
            ap.error("--bundle takes K >= 2 (1 is no bundling at all)")
        if scen == "codegen":
            ap.error("--bundle is for code_execution / test_output_prediction:"
                     " one code_generation problem is already a whole program")
    if args.harden_target is not None and not (0 < args.harden_target <= 100):
        ap.error("--harden-target is a percentage between 0 and 100")
    args.skills_parsed = None
    if args.skills:
        from .suite_data import SKILLS
        args.skills_parsed = [s.strip() for s in args.skills.split(",") if s.strip()]
        bad = [s for s in args.skills_parsed if s not in SKILLS]
        if bad:
            ap.error(f"--skills: unknown {', '.join(bad)} (use {', '.join(SKILLS)})")
    if args.mix:
        if not args.random_sample:
            ap.error("--mix is a share *of a sample*: give it --random-sample N too")
        try:
            args.mix_parsed = parse_mix(args.mix)
        except ValueError as e:
            ap.error(str(e))
        state.MIX_SPEC = args.mix

    if args.self_test:
        sys.exit(self_test())
    if args.pool_info:
        show_pool_info(args, scen)
        return

    os.makedirs("bench", exist_ok=True)
    if args.report_only:
        build_report()
        return
    if _run_management(args):
        return
    if args.set_harness:
        _set_harness(args, ap)
        return

    # ---- Who is being benched, and in which mode? Never guessed. -----------
    try:
        server_models = requests.get(f"{args.base_url}/models", timeout=10,
                                     headers=auth_headers()).json()
        model_id = args.model or server_models["data"][0]["id"]
    except Exception as exc:
        model_id = args.model
        print(f"Could not query {args.base_url}/models ({type(exc).__name__}):"
              " pass --base-url and/or --model if this is not llama-server on :8080.")
    if args.name:
        base = slugify(args.name)
    elif model_id:
        base = ask_label(slugify(model_id))
    else:
        ap.error("the server reported no model id and no --name was given: pass"
                 " --name <label> (and --model <id> if the server needs it).")

    kind = args.suite or scen          # key into SCEN_SUFFIX
    prev_harness = _prev_harness(base, kind)
    if args.both:
        passes = both_answers(base, kind, args.harness_note)
    else:
        if args.harness:
            harness_val, harness_note = args.harness, args.harness_note
            harness_src = "--harness flag"
        elif sys.stdin.isatty():
            stem = base + SCEN_SUFFIX[kind]
            folders = {mode: stem + HARNESS_SUFFIX[mode] for mode in HARNESS_SUFFIX}
            folders[None] = stem
            harness_val, harness_note, harness_src = ask_harness_mode(prev_harness, folders)
        else:
            harness_val, harness_note = prev_harness, None
            harness_src = ("kept from an earlier phase of this run" if prev_harness
                           else "not answered (detached run) - use --harness yes|no")
        passes = [(harness_val, harness_note, harness_src)]

    clock = None
    if args.suite:
        from .suite import SUITES
        total_min = args.budget_min or SUITES[args.suite]["budget_min"]
        clock = {"total_s": total_min * 60.0, "start": time.time(),
                 "passes": len(passes), "pass_index": 0}
        print(f"\nSuite {args.suite}: {total_min} min of generation in total"
              f" over {len(passes)} pass(es), plus grading and dataset loading.")

    done = []
    try:
        # passes come ordered: the no-harness one first, so it really runs alone
        for i, (harness_val, harness_note, harness_src) in enumerate(passes):
            if args.suite:
                from .suite import run_suite
                clock["pass_index"] = i
                got = run_suite(args, args.suite, base, model_id, harness_val,
                                harness_note, harness_src, clock,
                                report_after=(len(passes) == 1))
            else:
                got = run_one(args, scen, base, model_id, harness_val, harness_note,
                              harness_src, report_after=(len(passes) == 1))
            if got:
                done.append(got)
    finally:
        if len(passes) > 1 and done:
            # the pair belongs together: show just these rows, not the whole
            # history (that is what --report-only is for)
            build_report(only=done)


def _prev_harness(base, kind):
    return recorded_harness(os.path.join("bench", base + SCEN_SUFFIX[kind]))
