# EXTRA_MINIMAL_BENCHMARK - bench any local model, copy-paste only

Every run prints one **SCORE** (% solved, mean of easy / medium / hard / hardest cells),
the read and write speed in tok/s, and lands as one row in `bench\report.md`.
Run everything from the **project root** (the folder holding `lcb_bench.py` and
`lcbbench\`) in a **normal PowerShell window** (agent/IDE sandboxes block the grader's
subprocesses: `WinError 5 / Access is denied`).

**One test = two rows**: `--both` runs the same items twice.
- pass 1 = the **raw model** (`...-noharness`), nothing else on the model server.
- pass 2 = **solved through DeepSeek Harness** (`...-harness`, shown as `yes-agent` in the
  report): every item goes through `dsh --profile headless` with its full default
  toolset (shell, editor, file tools, planning, sub-agents, skills, context compaction)
  against the same llama-server, then the agent's final message is graded by the same
  grader as a raw answer. Pass 2 replays exactly the items pass 1 finished.

**Rerunning the same command for the same model does NOT start a new test.** It resumes the
folders in `bench\`: finished items are kept and a finished run adds nothing new. So the
first question you get is **Q0**: "this model already has results, start fresh / resume /
cancel". Fresh moves the old folders to `bench\_archive` (nothing is erased, `--restore-run`
brings them back). Without a keyboard (detached) it resumes; force it with `--fresh` or
`--resume`. Crashes and closed windows are cheap: rerun the same command and answer Resume.

## 0. Install the update and launch the env

```powershell
# once: unzip lcb_bench_update.zip over the project root (keeps bench\ untouched)
Expand-Archive -Force lcb_bench_update.zip .
# only if .venv does not exist yet:
#   uv venv --python 3.11
#   uv pip install -e .
.venv\Scripts\Activate.ps1
# optional, lifts the Hugging Face download rate limit:
#   $env:HF_TOKEN = "hf_xxx"
# once, for the harness pass (needs Node.js 22 or newer):
npm install -g @deepseek-ai/dsh
dsh --version
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

Tool calling must work on the server (llama.cpp has `--jinja` on by default in recent builds).

## 2b. Harness check (once per model server, 1 minute)

```powershell
python lcb_bench.py --harness-check
```

It finds dsh, loads the bench overlay and runs one real task: the agent must run a shell
command and answer. Expected: `smoke task: 2 step(s), 1 tool call(s) (1 ok, 0 error) ... answer 42 (correct)`.
`--both` repeats this check automatically before pass 2 (`--skip-harness-check` skips it).

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
- Prints SCORE, a skill x tier table, prefill and decode tok/s, truncation, and for the
  harness pass the agent line (steps, tool calls per tool, tool errors, timeouts).
- The raw pass gets about a third of the time, the agent pass the rest (the agent needs
  roughly twice as long per item and replays only the items the raw pass finished).
- At the end of pass 2 you get a **paired comparison**: raw vs agent on exactly the items
  both passes finished, per skill and tier. Compare the passes there, not by the two SCORE
  numbers (they can cover different cells). One item is worth several points at 2 items per
  cell, so differences of a few points are noise.
- The harness pass is slower per item (several model steps, a large agent prompt, tool
  time) and gets what is left of the budget, so its row may hold fewer `problems`. Compare
  the two rows on the same items with `--rounds N`, or raise `--budget-min`.

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

## 4b. How the harness pass works (and its knobs)

Per item: a fresh empty temp directory, `dsh --profile headless --json` with the task on
stdin (the same prompt as the raw pass plus a short evaluation note), the bench overlay
(a provider pointing at your `--base-url` with the server's model id, session titles off,
web tools off), `DSH_PERMISSION_MODE=danger-full-access`, telemetry off, a clean
`bench\_dsh_home`. The raw event log of every item is kept in `bench\<run>\harness_events\`.

| flag | what it does |
|---|---|
| `--harness-mode raw` | old behaviour: the raw model answers, an agent chat just shares the server |
| `--harness-access workspace` | workspace-write sandbox instead of full access (the Windows sandbox can fail) |
| `--harness-web` | keep web_search / web_fetch (off by default: benchmark answers are on the web) |
| `--harness-home DIR` | use your own `~/.dsh` (your AGENTS.md, skills, MCP) instead of the clean home |
| `--harness-patch FILE` | extra dsh patch overlay (repeatable), e.g. add an MCP server |
| `--harness-timeout MIN` | kill an item after MIN minutes (default 20 fast, 40 long) |
| `--harness-cmd CMD` | how to start dsh (default: `dsh` on PATH, else npx; or env `DSH_CMD`) |

If the agent finishes without the required answer format (for example it saved the solution
to a file and only said so), the runner sends ONE reminder in the same session ("reply now with
only the final answer in the format asked"). The run prints how many reminders were needed and
how many items still lacked the format (those score as wrong).

Things to know:
- Full access means the agent can touch anything on this PC. It starts in an empty temp
  directory and is told to stay there, but that is an instruction, not a wall.
- The run warns when an item used web tools or touched dataset caches / bench files
  (`WARNING: ... look like a lookup`). Look at `harness_events\` before trusting such a row.
- The raw pass sends `temperature 0.2` (`--temperature`); dsh sends none, so the server's own
  `--temp` applies. To equalize, run with `--temperature <your server --temp>`.
- 3 items in a row that cannot run through dsh stop the pass with the reason.

## 5. Report

```powershell
python lcb_bench.py --report-only
```

Prints every run side by side, best first, and writes `bench\report.md` + `bench\report.csv`.
A finished run prints its own row (or the two rows of a `--both` pair).
Columns: SCORE, problems, cap, trunc%, easy / medium / hard / hardest, code / math /
science / reading, prefill-tok/s, gen-tok/s, gen-min. `harness` shows `no` (raw model), `yes-agent` (solved through dsh) or `yes` (old raw rows).
Rows are comparable only when
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
| one pass only | `--suite fast` (asks raw or agent) or `--suite fast --harness yes` (agent pass, no `--both`) |
| agent pass does not start | `python lcb_bench.py --harness-check` shows why (dsh missing, server down, no tool calling) |
| a run died or the window closed | rerun the exact same command, answer Resume (Q0) |
| redo a test for the same model | answer Start fresh at Q0, or add `--fresh` |
| harness column empty on an old run | `python lcb_bench.py --set-harness yes --name <run>` |
| raw answers of a run | `bench\<run>\generations.jsonl` |
| which items a run finished | `bench\<run>\suite_items.json` |
| what the agent did on an item | `bench\<run>\harness_events\<item>.jsonl` (steps, tool calls, final message) |
| the run's numbers | `bench\<run>\_summary.json` (cells, skills, tiers, speeds) |
| quick, narrow ceiling checks | `--scenario code_execution --bundle 3 --random-sample 100` (old fast tests, they saturate) |
| any other flag | `python lcb_bench.py --help` |

## Where to look for the result

- The final `SCORE` block of the run (table skill x tier, speeds, harness proof).
- `bench\report.md` + `bench\report.csv` - all runs in one table.
- `bench\_archive\<stamp>__<name>` - runs you took out of the report.
