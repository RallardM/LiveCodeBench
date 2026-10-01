"""One classic bench pass: probe, pick problems, generate, extract, grade,
write bench/<run>/_summary.json and print that run's row. Used for the single
scenarios (code_generation, code_execution, test_output_prediction).
The one-command suites live in suite.py."""

import datetime as dt
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import state
from .advice import saturation_note
from .console import console_tick
from .extract import (extract_code, extract_exec_answer, extract_exec_bundle_answers,
                      extract_top_answer, extract_top_bundle_answers)
from .generate import (ensure_speed_probe, generate_one, generation_stats,
                       load_gen_checkpoint, speed_summary)
from .grading import eval_one_problem
from .harness import begin_pass, end_probe, harness_line, resolve_run_dir
from .loaders import load_exec_problems, load_problems, load_top_problems
from .prompts import format_prompt
from .report import build_report
from .scoring import compute_pass_metrics, count_pass


def run_one(args, scen, base, model_id, harness_val, harness_note,
            harness_src, report_after=True):
    """One bench pass, end to end. Called once normally and twice with --both
    (the no-harness pass, then the with-harness pass). Returns the run folder
    name, or None when the pass stopped before producing anything."""
    run_slug = resolve_run_dir(base, scen, harness_val)
    run_dir = os.path.join("bench", run_slug)
    harness_note, harness_probe, _ = begin_pass(
        run_slug, run_dir, model_id, harness_val, harness_note, harness_src)

    if args.speed_probe:
        ensure_speed_probe(args.base_url, model_id, run_dir)

    if args.probe_only:
        print("--probe-only: stopping here, nothing generated. The same run resumed"
              " (same label, same harness answer) reuses the saved probe"
              " (bench/<run>/speed_probe.json).")
        return None

    sample_file = os.path.join(run_dir, "sample_ids.json")
    mix = getattr(args, "mix_parsed", None)
    bundle = int(getattr(args, "bundle", 1) or 1)
    if scen == "codegen":
        problems = load_problems(args.release, args.start_date, args.end_date,
                                 args.difficulty, args.limit,
                                 args.random_sample, sample_file,
                                 hardest=args.hardest, mix=mix)
    elif scen == "exec":
        problems = load_exec_problems(args.release, args.start_date, args.end_date,
                                      args.difficulty, args.limit,
                                      args.random_sample, sample_file,
                                      cot=args.exec_cot, hardest=args.hardest,
                                      mix=mix, bundle=bundle)
    else:
        problems = load_top_problems(args.release, args.start_date, args.end_date,
                                     args.difficulty, args.limit,
                                     args.random_sample, sample_file,
                                     hardest=args.hardest, mix=mix, bundle=bundle)
    if bundle > 1 and problems and not problems[0].get("bundle"):
        print("WARNING: --bundle given but the sample is not bundled items")
    if bundle > 1:
        print(f"Bundle: every item is {bundle} calls answered in ONE go -"
              f" all {bundle} right or the item fails (score scales roughly"
              f" like per-call-rate^{bundle}, that is where the headroom is)")
    qorder = [p["question_id"] for p in problems]
    probs_by_id = {p["question_id"]: p for p in problems}
    gen_path = os.path.join(run_dir, "generations.jsonl")

    # ---- Phase 1: generate -----------------------------------------------
    if not args.skip_generate:
        done = load_gen_checkpoint(gen_path)
        if done:
            print(f"Resume: {len(done)} generations already done")
        tasks = []
        for p in problems:
            messages = p.get("messages") or format_prompt(
                p["question_content"], p["starter_code"])
            for si in range(args.n):
                if (p["question_id"], si) not in done:
                    tasks.append((p["question_id"], si, messages))
        if tasks:
            payload_extras = {
                "temperature": args.temperature,
                "top_p": args.top_p,
                "max_tokens": args.max_tokens,
            }
            if args.extra_body:
                payload_extras.update(json.loads(args.extra_body))
            print(f"Generating {len(tasks)} outputs with {args.workers} worker(s)...")
            t0 = time.time()
            gen_count = 0
            with open(gen_path, "a", encoding="utf-8") as out:
                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futures = [
                        pool.submit(generate_one, t, args.base_url, model_id,
                                    payload_extras, 3, args.request_timeout)
                        for t in tasks
                    ]
                    for fut in futures:
                        rec = fut.result()
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out.flush()
                        gen_count += 1
                        tok, secs = rec.get("completion_tokens"), rec.get("seconds")
                        tps = f" {tok / secs:6.1f} tok/s" if (tok and secs) else ""
                        fail = " FAILED" if rec.get("failed") else ""
                        print(f"[gen {gen_count}/{len(tasks)}] {rec['qid']}#{rec['si']} "
                              f"{secs}s{tps}{fail}", flush=True)
                        console_tick(run_dir)
            print(f"Generation phase done in {(time.time() - t0) / 60:.1f} min")

    done = load_gen_checkpoint(gen_path)

    # ---- Phase 2: extract --------------------------------------------------
    gen_map = {}
    for (qid, si), rec in done.items():
        gen_map.setdefault(qid, {})[si] = rec["output"]
    generations = []
    compat = []
    missing = []
    for qid in qorder:
        outs_map = gen_map.get(qid)
        if not outs_map:
            # No generation for this problem yet (interrupted run / failed
            # requests). Grading it as an empty answer would poison the score,
            # so it is left out and reported instead.
            missing.append(qid)
            continue
        outs = [outs_map.get(si, "") for si in range(args.n)]
        if scen == "codegen":
            codes = [extract_code(o, args.extractor) for o in outs]
        elif scen == "exec" and bundle > 1:
            codes = [extract_exec_bundle_answers(o, probs_by_id[qid].get("inputs") or [],
                                                 args.exec_cot) for o in outs]
        elif scen == "exec":
            codes = [extract_exec_answer(o, args.exec_cot) for o in outs]
        elif bundle > 1:
            codes = [extract_top_bundle_answers(o, probs_by_id[qid].get("calls") or [])
                     for o in outs]
        else:
            codes = [extract_top_answer(o) for o in outs]
        generations.append({"question_id": qid, "output_list": outs, "code_list": codes})
        compat.append({"question_id": qid,
                       "code_list": [c if isinstance(c, str) else json.dumps(c)
                                     for c in codes]})
    if missing:
        print(f"WARNING: {len(missing)}/{len(qorder)} problems have no generation yet "
              f"(rerun the same command to generate them) - they are NOT counted "
              f"in the score.")
    with open(os.path.join(run_dir, "generations.json"), "w", encoding="utf-8") as f:
        json.dump(generations, f, indent=2, ensure_ascii=False)
    with open(os.path.join(run_dir, "results_official_format.json"), "w",
              encoding="utf-8") as f:
        json.dump(compat, f, indent=2, ensure_ascii=False)
    print(f"Wrote generations.json ({len(generations)} problems, n={args.n})")

    # ---- Phase 3: evaluate ---------------------------------------------------
    eval_path = os.path.join(run_dir, "eval_results.jsonl")
    evaluated = {}
    if os.path.exists(eval_path):
        with open(eval_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    evaluated[rec["qid"]] = rec
        if evaluated:
            print(f"Resume: {len(evaluated)} problems already evaluated")

    jobs = []
    if not args.skip_eval:
        for entry in generations:
            qid = entry["question_id"]
            if qid in evaluated:
                continue
            # codegen problems carry "in_out"; the fast scenarios only "eval_payload".
            payload = probs_by_id[qid].get("eval_payload") or probs_by_id[qid].get("in_out")
            if payload is None:
                sys.exit(f"Internal error: problem {qid} has no eval payload "
                         f"(keys: {sorted(probs_by_id[qid])})")
            jobs.append((qid, payload,
                         entry["code_list"], args.timeout,
                         (scen + "b") if (bundle > 1 and scen in ("exec", "top"))
                         else scen))

    if jobs:
        print(f"Evaluating {len(jobs)} problems with {args.eval_workers} eval "
              f"workers ({args.timeout}s/test budget)...")
        t_eval = time.time()
        with open(eval_path, "a", encoding="utf-8") as out:
            with ThreadPoolExecutor(max_workers=args.eval_workers) as pool:
                done_ct = 0
                for rec in pool.map(eval_one_problem, jobs):
                    evaluated[rec["qid"]] = rec
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                    done_ct += 1
                    if done_ct % 10 == 0 or done_ct == len(jobs):
                        print(f"[eval {done_ct}/{len(jobs)}] "
                              f"elapsed {(time.time() - t_eval) / 60:.1f} min", flush=True)
                    console_tick(run_dir)
    else:
        print("Evaluation already complete for current problem set")

    # ---- Phase 4: metrics ------------------------------------------------------
    per_problem, per_diff = [], {}
    per_problem_pass = {}
    sub_pass, sub_total = 0, 0  # --bundle: per-call (not per-item) counters
    for qid in qorder:
        if qid not in gen_map:
            continue  # stale verdict for a problem with no (kept) generation
        rec = evaluated.get(qid)
        if not rec:
            continue
        n, c = count_pass(rec)
        for one in rec.get("sub") or []:
            if isinstance(one, list):
                sub_total += len(one)
                sub_pass += sum(1 for x in one if x)
        per_problem.append((n, c))
        per_problem_pass[qid] = c / n if n else 0.0
        diff = probs_by_id[qid]["difficulty"].lower()
        per_diff.setdefault(diff, []).append((n, c))

    metrics = compute_pass_metrics(per_problem)
    diff_metrics = {d: compute_pass_metrics(v) for d, v in sorted(per_diff.items())}
    # The --hardest tier on its own: without this a run of 25 hardest inside 100
    # problems only ever shows one blended "hard" number.
    hardest_rates = [per_problem_pass[qid] for qid in per_problem_pass
                     if str(qid) in state.HARDEST_IDS]
    hardest_m = {"n": len(hardest_rates),
                 "pass@1": round(sum(hardest_rates) / len(hardest_rates), 4)
                 if hardest_rates else None}

    gs = generation_stats(done, gen_path, args.max_tokens, per_problem_pass, len(missing))
    speed = speed_summary(done, run_dir)
    summary_path = os.path.join(run_dir, "_summary.json")
    end_probe(harness_val, harness_note, harness_probe)
    bundle_m = None
    if bundle > 1 and sub_total:
        bundle_m = {"k": bundle, "calls": sub_total,
                    "per_call_pass": round(sub_pass / sub_total, 4)}
    summary = {
        "name": run_slug,
        "model": model_id,
        "server_model": model_id,
        "harness": harness_val or "unknown",
        "harness_note": harness_note,
        "harness_source": harness_src,
        "harness_probe": harness_probe or None,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "config": {
            "n": args.n, "temperature": args.temperature, "top_p": args.top_p,
            "max_tokens": args.max_tokens, "release": args.release,
            "start_date": args.start_date, "end_date": args.end_date,
            "difficulty": args.difficulty, "limit": args.limit or None,
            "random_sample": args.random_sample or None,
            "hardest": args.hardest or None,
            "mix": args.mix or None,
            "sample_note": state.SAMPLE_NOTE or None,
            "extractor": args.extractor, "eval_timeout": args.timeout,
            "scenario": scen, "exec_cot": bool(args.exec_cot),
            "bundle": bundle if bundle > 1 else None,
            "problems_total": len(per_problem),
        },
        "sample_hash": hashlib.sha1(",".join(str(q) for q in sorted(
            str(p["question_id"]) for p in problems)).encode()).hexdigest()[:12],
        "counts": {d: len(v) for d, v in sorted(per_diff.items())},
        "hardest_tier": hardest_m if hardest_m["n"] else None,
        "bundle_tier": bundle_m,
        # What the sample was drawn from: rows, questions, labels, date range.
        "pool": state.POOL_FACTS or None,
        "generation_stats": gs,
        # SINGLE HEADLINE VALUE: pass@1 % on the sampled problem set.
        "score": round(metrics.get("pass@1", 0.0) * 100, 2)
        if metrics.get("pass@1") is not None else None,
        "metrics": metrics,
        "metrics_by_difficulty": diff_metrics,
        "speed": speed,
        "per_problem_pass": per_problem_pass,
    }
    if per_problem:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
    else:
        print("No evaluated problems; summary not written (probe saved if run).")

    print("\n=== Results ===")
    print(f"   model: {model_id or 'unknown'}   run: bench\\{run_slug}")
    if not per_problem:
        print("Nothing evaluated yet.")
    if summary.get("score") is not None:
        print(f"SCORE: {summary['score']}%  "
              f"({len(per_problem)} problems, {args.n} sample(s) each) "
              f"[sample {summary['sample_hash']}"
              + (f", {state.SAMPLE_NOTE}" if state.SAMPLE_NOTE else "") + "]")
    for k, v in metrics.items():
        print(f"{k:>8}: {v * 100:.2f}%")
    for d, mm in diff_metrics.items():
        if "pass@1" in mm:
            print(f"  {d:>7}: {mm['pass@1'] * 100:.2f}%  ({len(per_diff[d])} problems)")
    if hardest_m["n"] and hardest_m["n"] < len(per_problem):
        print(f"  hardest: {hardest_m['pass@1'] * 100:.2f}%"
              f"  ({hardest_m['n']} problems of the --hardest tier)")
    if bundle_m:
        print(f"  per-call: {bundle_m['per_call_pass'] * 100:.2f}% over"
              f" {bundle_m['calls']} single calls inside the"
              f" {bundle_m['k']}-call items (item = all {bundle_m['k']} right)")
    if summary.get("score") is not None:
        _sc = summary["score"]
        if _sc >= 99.0 or (scen != "codegen" and _sc >= 90.0):
            print(saturation_note(scen, _sc, len(per_problem), per_diff,
                                  hardest_m, bundle_m=bundle_m,
                                  harden_target=args.harden_target))
    print_speed_lines(summary["speed"], gs, args.max_tokens)
    print(harness_line(harness_val, harness_src, harness_note, harness_probe))
    print(f"Summary: {summary_path}\n")

    if report_after:
        build_report(only=run_slug)
    return run_slug


def print_speed_lines(spd, gs, cap):
    """The read / write speed and truncation lines every run ends with."""
    if spd.get("decode_tokens_per_sec"):
        print(f"   speed: decode {spd['decode_tokens_per_sec']} tok/s, "
              f"prefill/lecture {spd['prefill_tokens_per_sec']} tok/s")
    if spd.get("mean_seconds_per_problem"):
        avg_p = spd.get("avg_prompt_tokens")
        avg_txt = f"avg {avg_p} prompt + " if avg_p else "avg ? prompt + "
        print(f"         {avg_txt}{spd['avg_completion_tokens']} completion tokens per problem, "
              f"{spd['mean_seconds_per_problem']}s/problem wall time")
    if gs.get("truncated_at_max_tokens") or gs.get("failed_requests"):
        print(f"   gen: {gs.get('truncated_at_max_tokens', 0)}/{gs.get('generated', 0)} "
              f"hit the {cap}-token cap (their answer is cut off"
              " mid-way - the partial answer is still graded, and it rarely passes), "
              f"{gs.get('failed_requests', 0)} failed request(s) "
              f"(rerun the same command to retry them)")
    if gs.get("pass_rate_when_not_truncated") is not None:
        print(f"      when-complete: {gs['pass_rate_when_not_truncated']}% of the "
              f"{gs.get('problems_not_truncated')}/{gs.get('generated')} answers that fit "
              f"in the token budget passed (skill signal, budget aside)")
