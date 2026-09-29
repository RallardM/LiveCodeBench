# EXTRA_MINIMAL_BENCHMARK — bench any local model, copy-paste only

Every run prints one **SCORE** = % of LiveCodeBench problems solved (plus read/write
speeds), and lands as one row in `bench\report.md`. Run everything below from the
**project root** (the folder holding `lcb_bench.py`) in a **normal PowerShell
window** — agent/IDE sandboxes block the grader's subprocesses
(`WinError 5 / Access is denied`).

**One test = two runs of the same command**, because a run is only comparable with
another one made the same way:

| you run it | from your own PowerShell window | from the agent chat |
|---|---|---|
| what to type | the command below | the same command + `--harness yes` |
| what it means | nothing else touches the model server | its requests queue behind the run, so wall time inflates. It never answers anything: both rows are the raw model |
| its row | `...-noharness` | `...-harness` |

**Or let one command do both passes**: add `--both` in your own window and it runs the
scenario twice on the same problems — pass 1 answered *no* (nothing else on the server),
pass 2 answered *yes* (the agent chat working) — and prints both rows at the end. It asks
which harness it is once, before starting, and tells you to close the chat so pass 1
really runs alone.

Nothing is guessed here. In your own window the tool asks **three questions**: **Q1**
what to call the run (press Enter and it takes the model id straight from the server,
so every row says which model it is — a bare `1`/`2`/`3` is refused there, those answer
Q2), **Q2** which of the two modes it is (answer `1` or `2`), and if you say yes, **Q3**
which harness: **choice `[1]` is the harness address it knows — the agent that started
this shell, or the address your earlier runs used — so Enter is enough** and nothing has
to be typed; choice `[2]` takes a new address or a name, if you want to test another
one. The bench pings an address at the start and end of the run so the row carries proof
the agent was there; nothing in what you type is ever opened. From the agent chat itself
nothing can answer, and `--harness yes` alone is enough — it takes that same address
from the shell's environment or from your earlier runs.

A run answers its harness question **once** and writes **one** row. If you answered `2`
once, you got one row — nothing forced the second answer. `--both` is how you get both
rows from one command.

Crashes and closed windows are cheap: rerun the same command, it skips what is done.

## Every command below tests easy up to the hardest, in one go

`--random-sample 100 --hardest 25` is the **one** sampling you need. The run is exactly
100 problems: 25 of them are picked as the hardest the release has, the other 75 are
spread evenly over easy / medium / hard — so about **25 easy + 25 medium + 25 hard + 25
hardest** in a single total. Nothing to run twice, no separate "hard mode".

Why it is in every line: without it a sample is easy-weighted and a good model prints a
plain **100%**, which tells you nothing. The score line now also prints the hardest tier
on its own, and the report has a `hardest` column for it.

## 0. Launch the env (once per window)

```powershell
# only if .venv does not exist yet:
#   uv venv --python 3.11
#   uv pip install -e .
.venv\Scripts\Activate.ps1
```

## 1. Grader self-check (once, no server needed)

```powershell
python lcb_bench.py --self-test
```

`Self-test: PASSED` = the grader works on this machine.

## 2. Serve the model you want to test (default port 8080)

```powershell
# straight from Hugging Face (downloads the quant if missing)
llama-server -hf "user/Model-GGUF:QUANT" -ngl 99 -c 65536
# or from a .gguf file you already have
llama-server -m "path\to\your-model.gguf" -ngl 99 -c 65536
```

Keep `-c 65536` + `--max-tokens 16384` as a pair (on a `-c 32768` server use
`--max-tokens 12288`). Other servers: add `--base-url` — LM Studio
`http://localhost:1234/v1`, vLLM `http://localhost:8000/v1`, Ollama
`http://localhost:11434/v1`. The with-harness runs use the chat's own server.

## 3. The long test — the main number (hours, resumable)

```powershell
python lcb_bench.py --speed-probe --random-sample 100 --hardest 25 --workers 1 --max-tokens 16384 --eval-workers 8 --both
```

One command, both rows: it asks **Q1** what to call the run (Enter accepts the model id
from the server), then **which harness** shares the server for the second pass (Enter
takes the one it knows), then runs pass 1 alone and pass 2 while the chat is working.
The sample gets a new `sample` hash when you change the sampling, so old rows stay
separate and comparable on their own terms.

Without `--both` it is the older one-run-at-a-time shape: answer `1` here (nothing else
on the server) and run the same line again from the agent chat with `--harness yes`
appended — nothing else to add: the harness address comes from that shell's own
environment or from your earlier runs, and the bench pings it. Two rows, same problems.

Rough cost on a laptop GPU: ~12 h per run. Run it detached so a closed window does
not kill it:

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -RedirectStandardOutput "bench\long-run.out.log" -RedirectStandardError "bench\long-run.err.log" `
  -ArgumentList "--speed-probe","--random-sample","100","--hardest","25","--workers","1","--max-tokens","16384","--eval-workers","8","--harness","no"
Get-Content bench\long-run.out.log -Wait -Tail 20
```

(Detached runs cannot answer questions, so the label comes from the server and
`--harness` must be spelled out.)

## 4. The two fast tests — same pattern, much quicker

Answers here are one line, so these are cheap. They measure narrower skills, so the
report flags them `test = fast` (the long test is `slow`), and fast rows compare
**only** with fast rows of the same scenario. Same rule as above: your own window
answers the question, the agent chat says `--harness yes`.

```powershell
# mental execution: the model gets a small piece of code and one call like f(2, 3),
# and must write down what that call returns, without running it
python lcb_bench.py --scenario code_execution --speed-probe --random-sample 100 --hardest 25 --workers 1 --max-tokens 16384 --eval-workers 8 --both

# test-output prediction: the model gets a problem with starter code and one test
# input, and must write the exact output that input should produce
python lcb_bench.py --scenario test_output_prediction --speed-probe --random-sample 100 --hardest 25 --workers 1 --max-tokens 16384 --eval-workers 8 --both
```

`--both` is what makes the fast test write **two** rows (`...-noharness` and
`...-harness`) instead of one: without it a run answers the harness question once and
records one row, which is what happened the first time you ran it. The model never
talks to the harness either way — no problem is ever shown to it.

About a `100%` here: the long test is where difficulty bites — its pool holds **350 hard
problems**, so `--hardest 25` finds real ones (hard pass@1 measured 21–24% while
easy/medium sat at 86–100%). The fast exec release holds only **9 problems flagged
hard**, so its hardest tier has to borrow the meatiest mediums and a strong model still
sweeps it; the run says so in its own note (`25 hardest (9 hard, 16 medium)`). That is a
fact about the scenario, not about your model.

## 5. Total report of every run you did

```powershell
python lcb_bench.py --report-only
```

A finished run prints **its own row** (or the two rows of a `--both` pair) and says
where the full table is — `--report-only` is the command that prints everything.

Rows are comparable **only** when `test` / `scenario` / `problems` / `n` / `cap` /
`sample` all match. The `model` column says which model a row is; `run` is the
folder it lives in; `hardest` is the score of the hardest tier alone (`-` for runs made
before `--hardest` existed).

## 6. Delete a score from the total report

Step by step, in a PowerShell window in the project root. Deleting never erases: the
run's folder moves to `bench\_archive\<stamp>__<name>` and puts it back is one command.

**Step 1 — see what is in the report, numbered:**

```powershell
python lcb_bench.py --list-runs
```

That prints something like this — your names, numbers and dates will differ:

```
Runs in the report:
     1  qwen38-flash-next-q2-harness             73.0%  slow codegen   yes   100  d46433bd9f02 2026-09-23
     2  qwen38-flash-next-q2-noharness           69.0%  slow codegen   no    100  d46433bd9f02 2026-09-24
     3  unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness  100.0%  fast exec  yes   100  d29d79fc371c 2026-09-25
     4  qwen38-flash-next-q2-exec-harness         98.0%  fast exec      yes   100  d29d79fc371c 2026-09-24
     * = the run in progress. delete <number> takes one out of the report.
```

**Step 2 — take one out, by the number it just showed:**

```powershell
python lcb_bench.py --delete-run 3
```

It answers (your stamp and name differ):

```
deleted unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness: moved to bench\_archive\20260926-091003__unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness
nothing was erased - --restore-run (or 'restore' in the console) brings it
back; bench/report.md + .csv are rebuilt without it
```

You can use the name instead of the number (`--delete-run iq4_xs-exec` works on any
unique piece of a run's name). An ambiguous name is refused with "matches 2 of them"
rather than a guess, and the run being written right now (marked `*`) cannot be deleted.

**Step 3 — check that it is gone from the table:**

```powershell
python lcb_bench.py --report-only
```

**Step 4 — only if you changed your mind: put it back.** `--list-runs` also prints the
archive under `Archived runs (…)`, with its own numbering:

```powershell
python lcb_bench.py --restore-run 1
```

```
restored unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness -> bench\unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness, it counts in the report again
```

**While a run is going**, you can do all of this without stopping it: press a key in the
run's window and `bench>` answers between two problems. `help` lists the commands,
`list` numbers them, `delete 2` takes one out, `archive` shows what you deleted,
`restore 1` puts one back, `status` counts them, `quit` stops listening. `--manage`
opens the same console on its own, with no server and no model needed.

## Where to look for the result

- `SCORE` prints at the end of every run, with the model next to it, plus the hardest
  tier's own line.
- `bench\<run>\_summary.json` — that run's score, per-difficulty split, speeds.
- `bench\report.md` + `bench\report.csv` — all your runs in one table, best first.
- `bench\<run>\generations.jsonl` — the raw answers, if a score looks broken.
- `bench\<run>\hardest_ids.json` — which problems were the hardest tier of that run.
- `bench\_archive\<stamp>__<name>` — runs you took out of the report; `restore` puts
  one back. Nothing is ever erased.

Flags, troubleshooting, and long explanations: [README_MINIMAL.md](README_MINIMAL.md),
[MINIMAL_BENCHMARK.md](MINIMAL_BENCHMARK.md), [BENCHMARK_GUIDE.md](BENCHMARK_GUIDE.md).
