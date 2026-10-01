# EXTRA_MINIMAL_BENCHMARK — bench any local model, copy-paste only

One **SCORE** per run, one row in `bench\report.md`. Run everything from the
project root (folder holding `lcb_bench.py`) in a **normal PowerShell window**
(agent sandboxes block the grader's subprocesses). Commands only; every
explanation lives in [MINIMAL_BENCHMARK.md](MINIMAL_BENCHMARK.md),
[BENCHMARK_GUIDE.md](BENCHMARK_GUIDE.md), [README_MINIMAL.md](README_MINIMAL.md).

**One test = two rows**: the command from your window, and the same command +
`--harness yes` from the agent chat. Or add **`--both`** in your window and it
runs both passes itself. It asks a few questions on the way — Enter always
accepts what it suggests, and nothing is ever guessed.

## 0. Env (once per window)

```powershell
# first time only:  uv venv --python 3.11  ;  uv pip install -e .
.venv\Scripts\Activate.ps1
```

## 1. Grader self-check (once, no server)

```powershell
python lcb_bench.py --self-test     # -> Self-test: PASSED
```

## 2. Serve the model (port 8080)

```powershell
llama-server -hf "user/Model-GGUF:QUANT" -ngl 99 -c 65536
# or:  llama-server -m "path\to\model.gguf" -ngl 99 -c 65536
# keep -c 65536 and --max-tokens 16384 as a pair
```

## 3. Fast tests — bundled: K calls per answer, all right or the item fails

```powershell
# mental execution: 1 answer = 3 programs x 1 call each, all 3 right or the item fails
python lcb_bench.py --scenario code_execution --bundle 3 --random-sample 100 --hardest 25 --speed-probe --workers 1 --max-tokens 16384 --eval-workers 8 --both

# test-output prediction: 1 answer = 2 tests of the same problem, both right or the item fails
python lcb_bench.py --scenario test_output_prediction --bundle 2 --random-sample 100 --hardest 25 --speed-probe --workers 1 --max-tokens 16384 --eval-workers 8 --both
```

Still ≥ 90%? The run ends by printing the exact next harder command (the
harden ratchet). Manual knobs:

```powershell
# heavier answer unit - ceilings: exec K=2..6 holds 233/151/76/74/71 items, top K=2..3 holds 183/77
python lcb_bench.py --scenario code_execution --bundle 6 --random-sample 71 --workers 1 --max-tokens 16384 --eval-workers 8

# what can any pool give - no GPU touched
python lcb_bench.py --pool-info --scenario code_execution --random-sample 100
```

## 4. The long test — the main number (hours, resumable)

```powershell
python lcb_bench.py --speed-probe --random-sample 100 --hardest 25 --workers 1 --max-tokens 16384 --eval-workers 8 --both
```

## 5. Hardest on purpose (long test): 50% hardest + only new contests

```powershell
python lcb_bench.py --scenario code_generation --random-sample 100 --mix 50/25/15/10 --start-date 2025-01-01 --workers 1 --max-tokens 16384 --eval-workers 8
# same shape, a quarter of the cost:  --random-sample 40
```

## 6. Report

```powershell
python lcb_bench.py --report-only    # bench\report.md + .csv, best first
python lcb_bench.py --list-runs      # numbered
python lcb_bench.py --delete-run 3   # out of the report (bench\_archive\, never erased)
python lcb_bench.py --restore-run 1  # back in
```

## Odds and ends

| when | what to add / look at |
|---|---|
| from the agent chat | `--harness yes` |
| detached run | `--harness yes`/`--harness no` + `--name label` (Start-Process pattern: MINIMAL_BENCHMARK.md §2) |
| a run died | rerun the exact same command — it resumes |
| move the "too easy" bar | `--harden-target 80` (default: fast 90, long 99) |
| re-grade without the GPU | `--skip-generate` |
| what was the hardest tier | the score's own `hardest:` line, `bench\<run>\hardest_ids.json` |
| raw answers | `bench\<run>\generations.jsonl` |

A fast row is comparable only with the same scenario **and the same `bundle`
column** (`x3` = three calls per answer). Why those pools saturate and why the
long test is the ranking: MINIMAL_BENCHMARK.md §3 — short version: the exec
release is 92 questions from 2023, its 60 hardest rows measured **100%** on
this machine, and it **held 100% even bundled 4 calls/answer** (160 calls);
the ratchet then aims the widest net (`--bundle 6 --random-sample 71`).
`--mix` + `--start-date` is what ranks.
