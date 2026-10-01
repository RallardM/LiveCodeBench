# EXTRA_MINIMAL_BENCHMARK - bench any local model, copy-paste only

Every run prints one **SCORE** (% solved, mean of easy / medium / hard / hardest cells),
the read and write speed in tok/s, and lands as one row in `bench\report.md`.
Run everything from the **project root** (the folder holding `lcb_bench.py` and
`lcbbench\`) in a **normal PowerShell window** (agent/IDE sandboxes block the grader's
subprocesses: `WinError 5 / Access is denied`).

**One test = two rows**: `--both` runs the same items twice, pass 1 with nothing else on
the model server (`...-noharness`), pass 2 with the agent chat working
(`...-harness`, replays exactly the items pass 1 finished). Nothing is ever routed
through the agent, both rows are the raw model. Crashes and closed windows are cheap:
rerun the same command, it resumes.

## 0. Install the update and launch the env

```powershell
# only if .venv does not exist yet:
#   uv venv --python 3.11
#   uv pip install -e .
.venv\Scripts\Activate.ps1
# optional, lifts the Hugging Face download rate limit:
#   $env:HF_TOKEN = "hf_xxx"
```

## 1. Grader self-check (once, no server needed)

```powershell
python lcb_bench.py --self-test
```

`Self-test: PASSED` = grader and answer readers work on this machine.

## 2. Serve the model (default port 8080)

```powershell
llama-server -hf "user/Model-GGUF:QUANT" -ngl 99 -c 65536
# or from a .gguf file:
llama-server -m "path\to\your-model.gguf" -ngl 99 -c 65536
```

Other servers: add `--base-url` (LM Studio `http://localhost:1234/v1`, vLLM
`http://localhost:8000/v1`, Ollama `http://localhost:11434/v1`).

## 3. FAST test - all skills, easy to hardest, 2 hours max

```powershell
python lcb_bench.py --suite fast --both
```

- 4 skills x 4 tiers = 16 cells, one item per cell per round, easy tier first.
  - **code**: LiveCodeBench, hardest = newest and biggest hard problems
  - **math**: GSM8K, MATH-500 (L3-4), MATH-500 (L5), AIME 2026 + HMMT Feb 2026
  - **science**: SuperGPQA easy, middle, hard, hard calculation
  - **reading** (predict what code prints): 1, 2, 4, 6 calls per answer, all must be right
- 120 min of generation in total for both passes (pass 1 gets half). New items stop
  starting when the time is spent, what finished is graded, plus a few minutes of grading.
- Answer caps per tier: 3072 / 4096 / 6144 / 8192 tokens (`--max-tokens` can only lower them).
- Prints SCORE, a skill x tier table, prefill and decode tok/s, truncation.

```powershell
# knobs (all optional)
python lcb_bench.py --suite fast --both --budget-min 60          # shorter
python lcb_bench.py --suite fast --both --skills math,science    # fewer skills
python lcb_bench.py --suite fast --both --rounds 3               # exact same items for every model
python lcb_bench.py --suite fast --both --apex                   # hardest math = MathArena Apex (strong models)
```

If the table says `no item finished for: ...` the budget ended before round 1 was done:
raise `--budget-min` or lower `--max-tokens`. Compare rows only with about the same
`problems` count (or force it with `--rounds`).

## 4. LONG test - code generation, 5 hours max, the main number

```powershell
python lcb_bench.py --suite long --both
```

- LiveCodeBench code generation, 4 tiers, full answer cap (`--max-tokens`, default 16384).
- 300 min of generation in total for both passes. Same time-box, same table.
- `--budget-min N`, `--rounds N`, `--start-date 2025-01-01` (only newer contests) and
  `--skills code,math,science,reading` (widen it) work here too.

Detached, so a closed window does not kill it (nothing can be asked, so give the name):

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -RedirectStandardOutput "bench\long-run.out.log" -RedirectStandardError "bench\long-run.err.log" `
  -ArgumentList "lcb_bench.py","--suite","long","--both","--name","mymodel"
Get-Content bench\long-run.out.log -Wait -Tail 20
```

## 5. Report

```powershell
python lcb_bench.py --report-only
```

Prints every run side by side, best first, and writes `bench\report.md` + `bench\report.csv`.
A finished run prints its own row (or the two rows of a `--both` pair).
Columns: SCORE, problems, cap, trunc%, easy / medium / hard / hardest, code / math /
science / reading, prefill-tok/s, gen-tok/s, gen-min. Rows are comparable only when
`test` / `scenario` / `problems` / `n` / `cap` / `sample` match.

## 6. Delete a score from the total report

Deleting never erases: the run's folder moves to `bench\_archive\<stamp>__<name>` and
putting it back is one command. Run these in a PowerShell window in the project root.

**Step 1 - see what is in the report, numbered:**

```powershell
python lcb_bench.py --list-runs
```

```
Runs in the report:
     1  mymodel-fast-harness      41.2%  fast fast      yes    58  c13dd219018f 2026-09-29
     2  mymodel-fast-noharness    43.0%  fast fast      no     61  c13dd219018f 2026-09-29
     3  mymodel-long-noharness    35.0%  slow long      no     14  d96b09c9412b 2026-09-29
     * = the run in progress. delete <number> takes one out of the report.
```

**Step 2 - take one out, by the number it just showed:**

```powershell
python lcb_bench.py --delete-run 3
```

```
deleted mymodel-long-noharness: moved to bench\_archive\20260929-091003__mymodel-long-noharness
nothing was erased - --restore-run (or 'restore' in the console) brings it back; bench/report.md + .csv are rebuilt without it
```

The name works too (`--delete-run long-noharness` takes any unique piece). An ambiguous
name is refused ("matches 2 of them"), and the run being written right now (`*`) cannot
be deleted.

**Step 3 - check that it is gone from the table:**

```powershell
python lcb_bench.py --report-only
```

**Step 4 - only if you changed your mind, put it back.** `--list-runs` also prints the
archive under `Archived runs (...)` with its own numbering:

```powershell
python lcb_bench.py --restore-run 1
```

**While a run is going** you can do all of this without stopping it: press a key in the
run's window and `bench>` answers between two items. `help` lists the commands, `list`
numbers them, `delete 2` takes one out, `archive` shows what you deleted, `restore 1`
puts one back, `status` counts them, `quit` stops listening. `--manage` opens the same
console on its own, no server needed.

## Odds and ends

| when | what to add / look at |
|---|---|
| from the agent chat | one pass only: `--suite fast --harness yes` (no `--both`) |
| a run died or the window closed | rerun the exact same command, it resumes |
| harness column empty on an old run | `python lcb_bench.py --set-harness yes --name <run>` |
| raw answers of a run | `bench\<run>\generations.jsonl` |
| which items a run finished | `bench\<run>\suite_items.json` |
| the run's numbers | `bench\<run>\_summary.json` (cells, skills, tiers, speeds) |
| quick, narrow ceiling checks | `--scenario code_execution --bundle 3 --random-sample 100` (old fast tests, they saturate) |
| any other flag | `python lcb_bench.py --help` |

## Where to look for the result

- The final `SCORE` block of the run (table skill x tier, speeds, harness proof).
- `bench\report.md` + `bench\report.csv` - all runs in one table.
- `bench\_archive\<stamp>__<name>` - runs you took out of the report.