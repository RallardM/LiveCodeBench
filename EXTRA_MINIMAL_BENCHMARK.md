# EXTRA_MINIMAL_BENCHMARK — bench any local model, copy-paste only

Every run prints one **SCORE** = % of LiveCodeBench problems solved (plus read/write speeds).
Run every command below from the **project root** (the folder holding `lcb_bench.py`) — all
paths here are relative to it. Use a **normal PowerShell window**: agent/IDE sandboxes block
the grader's subprocesses (`WinError 5 / Access is denied`).

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

## Without harness — step by step

### 2. Serve the model you want to test (default port 8080)

```powershell
# straight from Hugging Face (downloads the quant if missing)
llama-server -hf "user/Model-GGUF:QUANT" -ngl 99 -c 65536
# or from a .gguf file you already have
llama-server -m "path\to\your-model.gguf" -ngl 99 -c 65536
```

Keep `-c 65536` + `--max-tokens 16384` as a pair; on a `-c 32768` server use `--max-tokens 12288`.
Other servers: add `--base-url` to every bench command below — LM Studio `http://localhost:1234/v1`, vLLM `http://localhost:8000/v1`, Ollama `http://localhost:11434/v1`.

### 3. Speed probe — pick `RUN`, any short label you want

```powershell
python lcb_bench.py --name RUN --speed-probe --probe-only
```

### 4. Smoke test (optional): does the pipeline work end to end

```powershell
python lcb_bench.py --name RUN-smoke --limit 3 --max-tokens 16384 --eval-workers 8
Remove-Item -Recurse -Force bench\RUN-smoke
```

### 5. The real run (resumable: rerun the exact same command after any crash)

```powershell
python lcb_bench.py --name RUN --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

Detached, so closing the window does not kill it:

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -RedirectStandardOutput "bench\RUN.out.log" -RedirectStandardError "bench\RUN.err.log" `
  -ArgumentList "--name","RUN","--speed-probe","--random-sample","100","--workers","1","--max-tokens","16384","--eval-workers","8"
Get-Content bench\RUN.out.log -Wait -Tail 20
```

### 6. Total report of every run you did

```powershell
python lcb_bench.py --report-only
```

## With harness — step by step

Same env check as §0–1, but the server step is skipped: the bench uses the chat's own
server, so you can only bench that model, and restarting that server kills the chat.
Chat and bench share one slot, so they queue behind each other.

```powershell
# run (add --harness yes from your own PowerShell; auto-detected from the agent shell)
python lcb_bench.py --name RUN --harness yes --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

```powershell
# report
python lcb_bench.py --report-only
```

## Optional extra scenarios (much faster, narrower skills)

```powershell
# mental execution: predict what a given function call returns
python lcb_bench.py --name RUN-exec --scenario code_execution --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8

# test-output prediction: write the full assert f(...) == <output>
python lcb_bench.py --name RUN-top --scenario test_output_prediction --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

## Where to look for the result

- `SCORE` prints at the end of every run.
- `bench\RUN\_summary.json` — that run's score, per-difficulty split, speeds.
- `bench\report.md` + `bench\report.csv` — all your runs in one table, best first.
- `bench\RUN\generations.jsonl` — the raw answers, if a score looks broken.

## Flags you may actually need

| Flag | Why |
|---|---|
| `--name` | required: your run label → `bench\<name>\`, resume key, report row |
| `--base-url` / `--api-key` / `--model` | a server that is not llama-server on 8080 |
| `--max-tokens` | thinking + answer budget (16384); truncated answers auto-fail |
| `--random-sample 100` | the fixed shared problem set → comparable scores |
| `--limit N` | first N problems only (quick check) |
| `--workers N` | concurrent requests; keep 1 on a `-np 1` server |
| `--n K` | K samples per problem, SCORE averages them (K× the time) |
| `--difficulty easy\|medium\|hard` | subset by difficulty |
| `--skip-generate` / `--skip-eval` | re-grade stored answers / generate only |
| `--extra-body JSON` | per-request extras, e.g. thinking off: `'{"chat_template_kwargs":{"enable_thinking":false}}'` |
| `--exec-cot` | step-by-step prompt for `code_execution` |
| `--harness yes\|no` | record that the chat was open on the same server (wall time inflated) |
| `--set-harness yes\|no --name RUN` | backfill that column for an old run |

Longer versions: [README_MINIMAL.md](README_MINIMAL.md), [MINIMAL_BENCHMARK.md](MINIMAL_BENCHMARK.md), [BENCHMARK_GUIDE.md](BENCHMARK_GUIDE.md).
