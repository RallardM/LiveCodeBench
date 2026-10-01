"""What a pool can give and what to run next: --pool-info, and the note printed
under a score that came out too easy (the harden ratchet)."""

from . import state
from .loaders import (DATASET_NAME, EXEC_DATASET_NAME, TOP_DATASET_NAME,
                      load_exec_problems, load_problems, load_top_problems)
from .sampling import mix_pick, parse_mix


def show_pool_info(args, scen):
    """--pool-info: what a scenario can actually be sampled from, with no model
    involved. A pool that holds no hard problems cannot hand out a hard sample."""
    label = {"codegen": DATASET_NAME, "exec": EXEC_DATASET_NAME,
             "top": TOP_DATASET_NAME}[scen]
    print(f"Pool for scenario {args.scenario} ({label}), release {args.release}:")
    if scen == "codegen":
        problems = load_problems(args.release, args.start_date, args.end_date,
                                 args.difficulty, 0)
    elif scen == "exec":
        problems = load_exec_problems(args.release, args.start_date, args.end_date,
                                      args.difficulty, 0, cot=args.exec_cot,
                                      bundle=args.bundle)
    else:
        problems = load_top_problems(args.release, args.start_date, args.end_date,
                                     args.difficulty, 0, bundle=args.bundle)
    facts = state.POOL_FACTS
    if not facts or not facts.get("n"):
        print("   nothing is left after the filters - no sample can be drawn at all")
        return
    labels = facts["labels"]
    print(f"   {facts['n']} problems from {facts['questions']} distinct questions"
          + ("" if facts["questions"] == facts["n"] else
             f"  ({facts['n'] / facts['questions']:.1f} rows per question:"
             " every row is ONE call, not the whole problem)"))
    print("   labels: " + ", ".join(f"{v} {k}" for k, v in sorted(labels.items())))
    hard_n = labels.get("hard", 0)
    print(f"   dates: {facts['date_min']} .. {facts['date_max']}"
          f"  -> {hard_n / facts['n'] * 100:.1f}% of the pool is labelled hard")
    print(f"   weight: median {facts['weight_median']}, biggest {facts['weight_max']}"
          f" (id {facts['biggest']}) - weight = test cases for code generation,"
          " characters of code / problem text for the fast scenarios")
    print("   what --mix can fill: hardest takes from all"
          f" {facts['n']}, then {hard_n} hard,"
          f" {labels.get('medium', 0)} medium, {labels.get('easy', 0)} easy - a"
          " share bigger than that gets refilled from the other labels")
    if args.random_sample:
        spec_text = args.mix or "50/25/15/10"
        spec = parse_mix(spec_text)
        got, _, note = mix_pick(problems, args.random_sample, spec, spec_text)
        print(f"   --random-sample {args.random_sample} --mix {spec_text} here:"
              f" {note}")
    if scen != "codegen":
        caps = facts.get("bundle_cap") or {}
        print("   what --bundle can do (K calls answered in ONE answer, all K"
              " right or the item fails):")
        for k in sorted(caps, key=int):
            if caps[k] >= 10:
                print(f"      K={k}: {caps[k]} items  (up to {int(k) * caps[k]}"
                      " single calls in play)")
        if not any(v >= 10 for v in caps.values()):
            print("      no K holds even 10 items - this pool cannot bundle")
        bk = facts.get("bundle_k")
        if bk:
            print(f"   (running now: --bundle {bk}, every item = {bk} calls,"
                  " all-or-nothing)")
        print("   verdict: every problem in this scenario is one call to one small"
              " function with a short answer. It is a ceiling check, not a"
              " discrimination test. For a ranking use --suite fast (all skills,"
              " easy to hardest) or --suite long (code generation).")


def _harden_ladder(scen, per_call, target, caps, cur_k=1, n_rows=0):
    """The harden ratchet: given the per-call pass rate just measured, which
    --bundle size would push the item score under `target` (p**k <= target), and
    does the pool actually hold that many items? Returns (lines, None) or
    (None, why_no_more_text)."""
    if not caps:
        return None, ("this pool cannot bundle at all - use --suite fast or"
                      " --suite long instead")
    scen_flag = "code_execution" if scen == "exec" else "test_output_prediction"
    lines = []
    k_order = sorted(int(k) for k, v in caps.items() if v >= 10)
    if cur_k:
        k_order = [k for k in k_order if k > cur_k]
    if per_call >= 0.99999:
        top_cov = max(k_order, key=lambda k: caps[str(k)] * k) if k_order else None
        if top_cov is None:
            return None, ("no bundle size of this pool holds >= 10 items"
                          " - use --suite fast or --suite long instead")
        cov = caps[str(top_cov)] * top_cov
        lines.append(
            f"every single call was right ({int(per_call * 100)}%): the answer"
            " unit has to carry more calls so any hidden miss fails a whole"
            f" item. The widest net this pool holds: --bundle {top_cov}"
            f" --random-sample {min(100, caps[str(top_cov)])} covers"
            f" {cov}/{n_rows} of every call in the release.")
        lines.append(
            f"python lcb_bench.py --scenario {scen_flag} --bundle {top_cov}"
            f" --random-sample {min(100, caps[str(top_cov)])} --workers 1"
            " --max-tokens 16384 --eval-workers 8")
        return lines, None
    for k in k_order:
        if per_call ** k <= target:
            n_samp = min(100, caps[str(k)])
            lines.append(
                f"per-call rate {per_call * 100:.1f}% -> a K-call item passes at"
                f" about {per_call * 100:.1f}%**K, so K={k} lands near"
                f" {per_call ** k * 100:.0f}% (target {target * 100:.0f}%):"
                f" --bundle {k}, and the pool holds {caps[str(k)]} items of"
                f" {k} calls.")
            lines.append(
                f"python lcb_bench.py --scenario {scen_flag} --bundle {k}"
                f" --random-sample {n_samp} --workers 1 --max-tokens 16384"
                " --eval-workers 8")
            return lines, None
    kmax = max(k_order) if k_order else None
    if kmax:
        lines.append(
            f"even K={kmax} items are expected at {per_call ** kmax * 100:.0f}% >"
            f" {target * 100:.0f}%: this pool is saturated for this model - use"
            " --suite fast or --suite long, they draw from harder public sets.")
        return lines, None
    return None, ("no bundle size of this pool holds >= 10 items"
                  " - use --suite fast or --suite long instead")


def saturation_note(scen, score, n_problems, per_diff, hardest_m,
                    bundle_m=None, harden_target=None):
    """Printed under a score of 99% or more (90% on the fast scenarios, which
    saturate that low): what that score was measured against and the next
    command that has a chance of being harder for THIS model."""
    facts = state.POOL_FACTS or {}
    labels = facts.get("labels") or {}
    target = ((harden_target if harden_target is not None
               else (90.0 if scen != "codegen" else 99.0)) / 100.0)
    pool_line = None
    if facts.get("n"):
        if facts.get("bundle_k"):
            lead = (f"{facts.get('raw_rows', '?')} rows from"
                    f" {facts.get('raw_questions', '?')} distinct questions,"
                    f" bundled {facts['bundle_k']} calls to an item ->"
                    f" {facts['n']} items")
        else:
            lead = (f"{facts['n']} rows from {facts['questions']}"
                    " distinct questions")
        pool_line = (f"      the pool it came from: {lead},"
                     f" {facts['date_min']} to {facts['date_max']},"
                     " labelled " + ", ".join(f"{v} {k}"
                                              for k, v in sorted(labels.items()))
                     + f"; biggest single row {facts['weight_max']}"
                     f" (median {facts['weight_median']})")
    hard_n = len(per_diff.get("hard", []))
    lines = []
    if scen == "codegen":
        if hard_n == n_problems:
            lines.append(f"      {score:g}% with all {n_problems} problems flagged hard:"
                         " memorised problems, not easy ones. Take the newest"
                         " contests: --start-date 2025-01-01 --hardest N")
        else:
            lines.append(f"      {score:g}% on {n_problems} problems where only {hard_n}"
                         " were hard: the sample cannot tell models apart. Try"
                         " --suite long, or pin the difficulty: --random-sample 100"
                         " --hardest 25, or --mix 50/25/15/10, plus --start-date"
                         " 2025-01-01")
        if pool_line:
            lines.append(pool_line)
    else:
        unit = (f" rows of {bundle_m['k']} calls" if bundle_m else " rows")
        lines.append(f"      {score:g}% on {n_problems}{unit} of the {scen} scenario:"
                     " read this as a ceiling check, not as a score. Every row here is"
                     " one or a few calls to small functions with short answers"
                     " printed back, and the whole release is old, so resampling it"
                     " cannot make it harder.")
        if pool_line:
            lines.append(pool_line)
        pc = (bundle_m["per_call_pass"] if bundle_m else score / 100.0)
        ladder, dead_end = _harden_ladder(
            scen, pc, target, facts.get("bundle_cap") or {},
            cur_k=(bundle_m["k"] if bundle_m else 1),
            n_rows=facts.get("raw_rows") or facts.get("n", 0))
        if ladder:
            lines.append("      harden it further (same rows, same grader - only the"
                         " answer unit gets heavier):")
            lines += [f"      {ln}" for ln in ladder]
        elif dead_end:
            lines.append(f"      {dead_end}")
        lines.append("      to compare models use the suites, they draw from harder"
                     " public sets and cover every skill:"
                     " python lcb_bench.py --suite fast --both")
    if hardest_m.get("n") and hardest_m.get("pass@1") is not None \
            and hardest_m["pass@1"] >= 0.9:
        lines.append(f"      even the hardest tier ({hardest_m['n']} problems) came"
                     f" out at {hardest_m['pass@1'] * 100:.0f}% - the pool had no"
                     " headroom left for this model within these filters")
    return "\n".join(lines)
