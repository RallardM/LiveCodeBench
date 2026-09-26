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

Crashes and closed windows are cheap: rerun the same command, it skips what is done.

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
python lcb_bench.py --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

It asks **Q1** what to call the run (Enter accepts the model id from the server) and
**Q2** the harness question — answer `1`. Then run that same line again from the agent
chat with `--harness yes` appended — nothing else to add: the harness address comes
from that shell's own environment or from your earlier runs, and the bench pings it.
Two rows, same 100 problems.

Rough cost on a laptop GPU: ~12 h per run. Run it detached so a closed window does
not kill it:

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -RedirectStandardOutput "bench\long-run.out.log" -RedirectStandardError "bench\long-run.err.log" `
  -ArgumentList "--speed-probe","--random-sample","100","--workers","1","--max-tokens","16384","--eval-workers","8","--harness","no"
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
python lcb_bench.py --scenario code_execution --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8

# test-output prediction: the model gets a problem with starter code and one test
# input, and must write the exact output that input should produce
python lcb_bench.py --scenario test_output_prediction --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

## 5. Total report of every run you did

```powershell
python lcb_bench.py --report-only
```

Rows are comparable **only** when `test` / `scenario` / `problems` / `n` / `cap` /
`sample` all match. The `model` column says which model a row is; `run` is the
folder it lives in.

## Where to look for the result

- `SCORE` prints at the end of every run, with the model next to it.
- `bench\<run>\_summary.json` — that run's score, per-difficulty split, speeds.
- `bench\report.md` + `bench\report.csv` — all your runs in one table, best first.
- `bench\<run>\generations.jsonl` — the raw answers, if a score looks broken.

Flags, troubleshooting, and long explanations: [README_MINIMAL.md](README_MINIMAL.md),
[MINIMAL_BENCHMARK.md](MINIMAL_BENCHMARK.md), [BENCHMARK_GUIDE.md](BENCHMARK_GUIDE.md).
