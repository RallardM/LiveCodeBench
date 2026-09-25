#!/usr/bin/env python
"""
lcb_bench.py — Fast, resumable, Windows-friendly LiveCodeBench harness for local
OpenAI-compatible model servers (llama.cpp / llama-server, LM Studio, vLLM, Ollama...).

Why this exists
---------------
* lcb_llama.py generated answers but never scored them — no pass@1, no test runs.
* It used raw question text with no prompt / format instructions, which badly
  hurts scores compared to the official LiveCodeBench chat prompt used on the
  official leaderboard (the GPT/Claude-style dialog format).
* The official lcb_runner in this repo cannot talk to llama.cpp (its OpenAI
  runner hardcodes api.openai.com) AND its evaluator uses signal.SIGALRM, which
  does not exist on Windows. This harness ports the same grading logic
  (grade_stdio / grade_call_based from lcb_runner/evaluation/testing_util.py)
  to a subprocess + parent-watchdog design that works natively on Windows.

Pipeline: generate -> extract code -> run the LCB test cases in sandboxed child
processes -> pass@k per difficulty + generation speed metrics -> report.

Everything checkpoints under bench/<model>/ so you can Ctrl+C or crash and just
re-run the same command.

Usage examples
--------------
  # 1) Start your llama-server for the model you want to test. For real speed
  #    use slots, e.g.:
  #      llama-server -m model.gguf -c 16384 --parallel 8 --threads <cores>
  #    (with -ngl 99 / --n-gpu-layers for GPU offload) then:
  python lcb_bench.py --name "qwen3-8b-instruct-q5" --workers 8

  # 2) Fast smoke test on 10 problems:
  python lcb_bench.py --name smoke --limit 10

  # 3) Thinking model, more samples:
  python lcb_bench.py --name "r1-distill-14b" --n 5 --max-tokens 32768 --workers 8 \
      --extra-body '{"chat_template_kwargs": {"enable_thinking": false}}'   # optional

  # 4) Re-run only evaluation of generations you already have:
  python lcb_bench.py --name "qwen3-8b-instruct-q5" --skip-generate

  # 5) Cross-model comparison table (also printed after every run):
  python lcb_bench.py --report-only

  # 6) Sanity-check the Windows evaluator (no model server needed):
  python lcb_bench.py --self-test

Outputs per model: bench/<name>/generations.jsonl (raw outputs, append-only),
bench/<name>/generations.json (official LCB output format),
bench/<name>/results_official_format.json (custom_evaluator-compatible, with
extracted code), bench/<name>/eval_results.jsonl, bench/<name>/_summary.json.
Cross-model table: bench/report.md + bench/report.csv.
"""

import argparse
import ast
import base64
import csv
import datetime as dt
import hashlib
import json
import multiprocessing
import os
import pickle
import re
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import StringIO
from types import ModuleType
from unittest.mock import mock_open, patch

import requests

try:  # some LCB test cases contain very large integers
    sys.set_int_max_str_digits(1_000_000)
except AttributeError:  # python < 3.11
    pass

DATASET_NAME = "livecodebench/code_generation_lite"

# -----------------------------------------------------------------------------
# Prompts (official LiveCodeBench OpenAIChat dialog style — verbatim, this is
# what scores on the public leaderboard are produced with)
# -----------------------------------------------------------------------------

SYSTEM_MESSAGE_GENERIC = (
    "You are an expert Python programmer. You will be given a question (problem "
    "specification) and will generate a correct Python program that matches the "
    "specification and passes all tests."
)
FORMATTING_MESSAGE_WITH_STARTER_CODE = (
    "You will use the following starter code to write the solution to the "
    "problem and enclose your code within delimiters."
)
FORMATTING_WITHOUT_STARTER_CODE = (
    "Read the inputs from stdin solve the problem and write the answer to stdout "
    "(do not directly test on the sample inputs). Enclose your code within "
    "delimiters as follows. Ensure that the python program runs, it reads the "
    "inputs, runs the algorithm and writes output to STDOUT."
)


def format_prompt(question_content: str, starter_code: str) -> list:
    prompt = f"### Question:\n{question_content}\n\n"
    if starter_code:
        prompt += f"### Format: {FORMATTING_MESSAGE_WITH_STARTER_CODE}\n"
        prompt += f"```python\n{starter_code}\n```\n\n"
    else:
        prompt += f"### Format: {FORMATTING_WITHOUT_STARTER_CODE}\n"
        prompt += "```python\n# YOUR CODE HERE\n```\n\n"
    prompt += "### Answer: (use the provided format with backticks)\n\n"
    return [
        {"role": "system", "content": SYSTEM_MESSAGE_GENERIC},
        {"role": "user", "content": prompt},
    ]


# -----------------------------------------------------------------------------
# Code extraction
# -----------------------------------------------------------------------------


def extract_code_official(model_output: str) -> str:
    """Exactly lcb_runner.utils.extraction_utils.extract_code for chat models."""
    outputlines = model_output.split("\n")
    indexlines = [i for i, line in enumerate(outputlines) if "```" in line]
    if len(indexlines) < 2:
        return ""
    return "\n".join(outputlines[indexlines[-2] + 1 : indexlines[-1]])


def extract_code_auto(model_output: str) -> str:
    """Official last-fence rule, with fallbacks for truncated / fence-less output."""
    if not model_output:
        return ""
    outputlines = model_output.split("\n")
    indexlines = [i for i, line in enumerate(outputlines) if "```" in line]
    if len(indexlines) >= 2:
        return "\n".join(outputlines[indexlines[-2] + 1 : indexlines[-1]])
    if len(indexlines) == 1:
        i = indexlines[0]
        before = "\n".join(outputlines[:i]).strip()
        after = "\n".join(outputlines[i + 1 :]).strip()
        head = outputlines[i].strip().lower()
        if head.startswith("```") and after:  # opening fence, closing truncated
            return after
        return before if before else after
    return model_output.strip()


def extract_code(model_output: str, extractor: str) -> str:
    if extractor == "official":
        return extract_code_official(model_output)
    return extract_code_auto(model_output)


# -----------------------------------------------------------------------------
# Dataset
# -----------------------------------------------------------------------------


def load_problems(release, start_date, end_date, difficulty, limit,
                  random_sample=0, sample_ids_file=None):
    # Imported lazily so spawned eval children don't pay this cost.
    from datasets import load_dataset

    try:
        ds = load_dataset(
            DATASET_NAME, split="test", version_tag=release, trust_remote_code=True
        )
    except Exception:
        ds = load_dataset(DATASET_NAME, split="test", trust_remote_code=True)

    problems = []
    for row in ds:
        meta = json.loads(row["metadata"])
        pub = json.loads(row["public_test_cases"])
        try:
            priv = json.loads(row["private_test_cases"])
        except Exception:
            priv = json.loads(
                pickle.loads(
                    zlib.decompress(
                        base64.b64decode(row["private_test_cases"].encode("utf-8"))
                    )
                )
            )
        in_out = json.dumps(
            {
                "inputs": [t["input"] for t in pub + priv],
                "outputs": [t["output"] for t in pub + priv],
                "fn_name": meta.get("func_name", None),
            }
        )
        contest_date = row["contest_date"]
        if isinstance(contest_date, str):
            contest_date = dt.datetime.fromisoformat(contest_date)
        problems.append(
            {
                "question_id": row["question_id"],
                "question_content": row["question_content"],
                "starter_code": row["starter_code"],
                "difficulty": row["difficulty"],
                "contest_date": contest_date,
                "in_out": in_out,
                "eval_payload": in_out,
                "kind": "codegen",
                "messages": format_prompt(row["question_content"], row["starter_code"]),
            }
        )
    return filter_sample(
        problems, start_date, end_date, difficulty, limit,
        random_sample, sample_ids_file,
    )


def filter_sample(problems, start_date, end_date, difficulty, limit,
                  random_sample=0, sample_ids_file=None):
    problems.sort(key=lambda p: str(p["question_id"]))
    if start_date:
        d = dt.datetime.strptime(start_date, "%Y-%m-%d")
        problems = [p for p in problems if p["contest_date"] >= d]
    if end_date:
        d = dt.datetime.strptime(end_date, "%Y-%m-%d")
        problems = [p for p in problems if p["contest_date"] <= d]
    if difficulty:
        problems = [
            p for p in problems if p["difficulty"].lower() == difficulty.lower()
        ]
    if limit:
        problems = problems[:limit]
    if random_sample:
        if sample_ids_file and os.path.exists(sample_ids_file):
            # resume: reuse the exact same subset chosen for this run
            with open(sample_ids_file, encoding="utf-8") as f:
                keep = {str(x) for x in json.load(f)}
            problems = [p for p in problems if str(p["question_id"]) in keep]
        elif len(problems) > random_sample:
            import random as _random

            rng = _random.Random(1234)
            groups = {}
            for p in problems:
                groups.setdefault(p["difficulty"].lower(), []).append(p)
            quotas = {d: random_sample * len(g) / len(problems) for d, g in groups.items()}
            alloc = {d: int(q) for d, q in quotas.items()}
            remaining = random_sample - sum(alloc.values())
            for d in sorted(quotas, key=lambda k: quotas[k] - int(quotas[k]), reverse=True):
                if remaining <= 0:
                    break
                alloc[d] += 1
                remaining -= 1
            picked = []
            for d, g in groups.items():
                picked += rng.sample(g, min(alloc[d], len(g)))
            picked.sort(key=lambda p: str(p["question_id"]))
            problems = picked
            if sample_ids_file:
                os.makedirs(os.path.dirname(sample_ids_file) or ".", exist_ok=True)
                with open(sample_ids_file, "w", encoding="utf-8") as f:
                    json.dump(sorted(str(p["question_id"]) for p in picked), f)
    print(f"Loaded {len(problems)} problems")
    return problems


# -----------------------------------------------------------------------------
# Extra scenarios (official LiveCodeBench "code_execution" and
# "test_output_prediction"). Prompts/scoring ported verbatim from
# lcb_runner/prompts/*.py and evaluation/compute_*.py.
# -----------------------------------------------------------------------------

EXEC_DATASET_NAME = "livecodebench/execution-v2"
TOP_DATASET_NAME = "livecodebench/test_generation"

EXEC_SYSTEM_MESSAGE = (
    "You are an expert at Python programming, code execution, test case "
    "generation, and fuzzing."
)
TOP_SYSTEM_MESSAGE = (
    "You are a helpful programming assistant and an expert Python programmer."
    " You are helping a user to write a test case to help to check the"
    " correctness of the function."
    " The user has written a input for the testcase."
    " You will calculate the output of the testcase and"
    " write the whole assertion statement in the markdown code block with the"
    " correct output."
)

EXEC_COT_PROMPT = """You are given a Python function and an assertion containing an input to the function. Complete the assertion with a literal (no unsimplified expressions, no function calls) containing the output when executing the provided code on the given input, even if the function is incorrect or incomplete. Do NOT output any extra information. Execute the program step by step before arriving at an answer, and provide the full assertion with the correct output in [ANSWER] and [/ANSWER] tags, following the examples.

[PYTHON]
def performOperation(s):
    s = s + s
    return "b" + s + "a"
assert performOperation(s = "hi") == ??
[/PYTHON]
[THOUGHT]
Let's execute the code step by step:

1. The function performOperation is defined, which takes a single argument s.
2. The function is called with the argument "hi", so within the function, s is initially "hi".
3. Inside the function, s is concatenated with itself, so s becomes "hihi".
4. The function then returns a new string that starts with "b", followed by the value of s (which is now "hihi"), and ends with "a".
5. The return value of the function is therefore "bhihia".
[/THOUGHT]
[ANSWER]
assert performOperation(s = "hi") == "bhihia"
[/ANSWER]

[PYTHON]
{code}
assert {inp} == ??
[/PYTHON]
[THOUGHT]
"""

EXEC_DIRECT_PROMPT = """You are given a Python function and an assertion containing an input to the function. Complete the assertion with a literal (no unsimplified expressions, no function calls) containing the output when executing the provided code on the given input, even if the function is incorrect or incomplete. Do NOT output any extra information. Provide the full assertion with the correct output in [ANSWER] and [/ANSWER] tags, following the examples.

[PYTHON]
def repeatNumber(number : int) -> int:
    return number
assert repeatNumber(number = 17) == ??
[/PYTHON]
[ANSWER]
assert repeatNumber(number = 17) == 17
[/ANSWER]

[PYTHON]
def addCharacterA(string : str) -> str:
    return string + "a"
assert addCharacterA(string = "x9j") == ??
[/PYTHON]
[ANSWER]
assert addCharacterA(string = "x9j") == "x9ja"
[/ANSWER]

[PYTHON]
{code}
assert {inp} == ??
[/PYTHON]
[ANSWER]
"""


def format_prompt_exec(code, inp, cot=False):
    tmpl = EXEC_COT_PROMPT if cot else EXEC_DIRECT_PROMPT
    return [{"role": "system", "content": EXEC_SYSTEM_MESSAGE},
            {"role": "user", "content": tmpl.format(code=code, inp=inp)}]


def parse_function_name_from_starter_code(starter_code):
    try:
        tree = ast.parse(starter_code)
    except SyntaxError:
        return None
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if fn is not None:
                return None
            fn = node.name
    return fn


def format_testcase_func_name_input(function_name, testcase):
    input_str = ", ".join(testcase.split("\n"))
    return f"assert {function_name}({input_str}) == # TODO"


def format_prompt_top(question_content, starter_code, function_name, testcase_input):
    func_name = parse_function_name_from_starter_code(starter_code) or function_name
    prompt = f"Problem:\n{question_content}"
    prompt += f"Function:\n```\n{starter_code}\n```\n"
    prompt += "Please complete the following test case:\n\n"
    prompt += f"```\n{format_testcase_func_name_input(func_name, testcase_input)}\n```\n"
    return [{"role": "system", "content": TOP_SYSTEM_MESSAGE},
            {"role": "user", "content": prompt}]


def load_exec_problems(release, start_date, end_date, difficulty, limit,
                       random_sample=0, sample_ids_file=None, cot=False):
    from datasets import load_dataset

    ds = load_dataset(EXEC_DATASET_NAME, split="test", trust_remote_code=True)
    problems = []
    seen = {}
    for row in ds:
        # execution-v2 has several rows (input/output pairs) sharing one
        # question_id; make every row unique so checkpoints + sampling are stable
        base = str(row["question_id"])
        idx = seen.get(base, 0)
        seen[base] = idx + 1
        contest_date = row["contest_date"]
        if isinstance(contest_date, str):
            contest_date = dt.datetime.fromisoformat(contest_date)
        problems.append(
            {
                "question_id": f"{base}#{idx}",
                "difficulty": row["difficulty"],
                "contest_date": contest_date,
                "kind": "exec",
                "eval_payload": json.dumps(
                    {"code": row["code"], "input": row["input"],
                     "output": row["output"]},
                    ensure_ascii=False,
                ),
                "messages": format_prompt_exec(row["code"], row["input"], cot),
            }
        )
    return filter_sample(
        problems, start_date, end_date, difficulty, limit,
        random_sample, sample_ids_file,
    )


def load_top_problems(release, start_date, end_date, difficulty, limit,
                      random_sample=0, sample_ids_file=None):
    from datasets import load_dataset

    ds = load_dataset(TOP_DATASET_NAME, split="test", trust_remote_code=True)
    problems = []
    for row in ds:
        tests = json.loads(row["test"])
        test = tests[0]
        contest_date = row["contest_date"]
        if isinstance(contest_date, str):
            contest_date = dt.datetime.fromisoformat(contest_date)
        problems.append(
            {
                "question_id": f"{row['question_id']}#{row['test_id']}",
                "difficulty": row["difficulty"],
                "contest_date": contest_date,
                "kind": "top",
                "eval_payload": json.dumps({"expected": test["output"]}),
                "messages": format_prompt_top(
                    row["question_content"], row["starter_code"],
                    row["function_name"], test["input"]
                ),
            }
        )
    return filter_sample(
        problems, start_date, end_date, difficulty, limit,
        random_sample, sample_ids_file,
    )


def extract_exec_answer(model_output, cot=False):
    # verbatim port of lcb_runner extract_execution_code
    if cot and "[ANSWER]" in model_output:
        model_output = model_output.split("[ANSWER]")[1].strip()
    if "==" in model_output:
        model_output = model_output.split("==")[1].strip()
    if "[/ANSWER]" in model_output:
        model_output = model_output.split("[/ANSWER]")[0].strip()
    else:
        model_output = model_output.split("\n")[0].strip()
    return model_output.strip()


def extract_top_answer(model_output):
    # verbatim port of lcb_runner extract_test_output_code (OpenAI style)
    outputlines = model_output.split("\n")
    indexlines = [i for i, line in enumerate(outputlines) if line.startswith("assert")]
    if indexlines:
        return outputlines[indexlines[-1]]
    indexlines = [
        i for i, line in enumerate(outputlines)
        if "```python" in line or "```Python" in line
    ]
    if indexlines:
        start_index = indexlines[0]
    else:
        start_index = None
    indexlines = [i for i, line in enumerate(outputlines) if "```" in line]
    if start_index is not None:
        indexlines = [i for i in indexlines if i > start_index]
        indexlines = [start_index] + indexlines
    if len(indexlines) < 2:
        return ""
    return "\n".join(outputlines[indexlines[0] + 1: indexlines[1]])


# -----------------------------------------------------------------------------
# Generation phase
# -----------------------------------------------------------------------------

_tls = None


def _session():
    global _tls
    import threading

    if _tls is None:
        _tls = threading.local()
    if not hasattr(_tls, "session"):
        _tls.session = requests.Session()
    return _tls.session


def generate_one(task, base_url, model_id, payload_extras, max_retries, read_timeout):
    qid, si, messages = task
    body = dict(payload_extras)
    body.update({"model": model_id, "messages": messages, "stream": False})
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.time()
            r = _session().post(
                f"{base_url}/chat/completions", json=body, timeout=(30, read_timeout)
            )
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            output = msg.get("content") or ""
            # llama.cpp >= b4599 returns thinking separately; if content empty use it
            if not output and msg.get("reasoning_content"):
                output = msg["reasoning_content"]
            usage = data.get("usage") or {}
            timings = data.get("timings") or {}  # llama.cpp server-side, queue-free
            return {
                "qid": qid,
                "si": si,
                "output": output,
                "seconds": round(time.time() - t0, 2),
                "completion_tokens": usage.get("completion_tokens"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "max_tokens": payload_extras.get("max_tokens"),
                "timings": {
                    "prompt_n": timings.get("prompt_n"),
                    "prompt_ms": timings.get("prompt_ms"),
                    "predicted_n": timings.get("predicted_n"),
                    "predicted_ms": timings.get("predicted_ms"),
                } if timings else None,
                "failed": False,
            }
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)[:400]
            time.sleep(min(60, 10 * attempt))
    return {
        "qid": qid, "si": si, "output": "", "seconds": None,
        "completion_tokens": None, "failed": True, "error": last_err,
    }


def load_gen_checkpoint(gen_path):
    done = {}
    if os.path.exists(gen_path):
        with open(gen_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                key = (rec["qid"], rec["si"])
                if not rec.get("failed"):
                    done[key] = rec
                else:
                    done.pop(key, None)  # failed entries don't count
    return done


def speed_probe(base_url, model_id):
    """Measure llama.cpp server-side prefill and decode speed with 2 requests.
    Prefill = 'lecture' speed (prompt processing tok/s); decode = generation tok/s.
    Uses server-side timings when available (excludes queue wait)."""
    filler = "The quick brown fox jumps over the lazy dog near the riverbank. " * 105
    out = {}
    try:
        body = {"model": model_id, "max_tokens": 1, "temperature": 0.0, "stream": False,
                "messages": [{"role": "user", "content": filler + "\nReply with exactly: OK"}]}
        t0 = time.time()
        d = _session().post(f"{base_url}/chat/completions", json=body, timeout=(30, 3600)).json()
        u, tg = d.get("usage") or {}, d.get("timings") or {}
        if tg.get("prompt_n") and tg.get("prompt_ms"):
            out["prefill_tokens_per_sec"] = round(tg["prompt_n"] / (tg["prompt_ms"] / 1000), 1)
        elif u.get("prompt_tokens"):
            out["prefill_tokens_per_sec"] = round(u["prompt_tokens"] / (time.time() - t0), 1)
    except Exception as e:  # noqa: BLE001
        out["prefill_error"] = repr(e)[:200]
    try:
        body = {"model": model_id, "max_tokens": 300, "temperature": 0.0, "stream": False,
                "messages": [{"role": "user",
                              "content": "Repeat exactly 300 times, space separated: ping"}]}
        t0 = time.time()
        d = _session().post(f"{base_url}/chat/completions", json=body, timeout=(30, 600)).json()
        u, tg = d.get("usage") or {}, d.get("timings") or {}
        if tg.get("predicted_n") and tg.get("predicted_ms"):
            out["decode_tokens_per_sec"] = round(tg["predicted_n"] / (tg["predicted_ms"] / 1000), 1)
        elif u.get("completion_tokens"):
            out["decode_tokens_per_sec"] = round(u["completion_tokens"] / (time.time() - t0), 1)
    except Exception as e:  # noqa: BLE001
        out["decode_error"] = repr(e)[:200]
    return out


# -----------------------------------------------------------------------------
# Evaluation engine — Windows-safe port of lcb_runner testing_util semantics.
# Each generated code is executed in its own spawned child process; the parent
# supervises with a per-message wall-clock budget (replacement for SIGALRM).
# -----------------------------------------------------------------------------

import_string = (
    "from string import *\nfrom re import *\nfrom datetime import *\n"
    "from collections import *\nfrom heapq import *\nfrom bisect import *\n"
    "from copy import *\nfrom math import *\nfrom random import *\n"
    "from statistics import *\nfrom itertools import *\nfrom functools import *\n"
    "from operator import *\nfrom io import *\nfrom sys import *\nfrom json import *\n"
    "from builtins import *\nfrom typing import *\nimport string\nimport re\n"
    "import datetime\nimport collections\nimport heapq\nimport bisect\nimport copy\n"
    "import math\nimport random\nimport statistics\nimport itertools\n"
    "import functools\nimport operator\nimport io\nimport sys\nimport json\n"
    "sys.setrecursionlimit(50000)\n"
)


def truncatefn(s, length=300):
    if isinstance(s, str):
        pass
    else:
        s = str(s)
    if len(s) <= length:
        return s
    return s[: length // 2] + "...(truncated) ..." + s[-length // 2 :]


class Capturing(list):
    def __enter__(self):
        self._stdout = sys.stdout
        sys.stdout = self._stringio = StringIO()
        self._stringio.close = lambda x: 1
        return self

    def __exit__(self, *args):
        self.append(self._stringio.getvalue())
        del self._stringio
        sys.stdout = self._stdout


class MockStdinWithBuffer:
    def __init__(self, inputs: str):
        self.inputs = inputs
        self._stringio = StringIO(inputs)
        self.buffer = MockBuffer(inputs)

    def read(self, *args):
        return self.inputs

    def readline(self, *args):
        return self._stringio.readline(*args)

    def readlines(self, *args):
        return self.inputs.split("\n")

    def __getattr__(self, name):
        return getattr(self._stringio, name)


class MockBuffer:
    def __init__(self, inputs: str):
        self.inputs = inputs.encode("utf-8")

    def read(self, *args):
        return self.inputs

    def readline(self, *args):
        return self.inputs.split(b"\n")[0] + b"\n"


def clean_if_name(code: str) -> str:
    try:
        astree = ast.parse(code)
        last_block = astree.body[-1]
        if isinstance(last_block, ast.If):
            condition = last_block.test
            if ast.unparse(condition).strip() == "__name__ == '__main__'":
                code = ast.unparse(astree.body[:-1]) + "\n" + ast.unparse(last_block.body)
    except Exception:
        pass
    return code


def make_function(code: str) -> str:
    try:
        import_stmts = []
        all_other_stmts = []
        astree = ast.parse(code)
        for stmt in astree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                import_stmts.append(stmt)
            else:
                all_other_stmts.append(stmt)
        function_ast = ast.FunctionDef(
            name="wrapped_function",
            args=ast.arguments(
                posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
            ),
            body=all_other_stmts,
            decorator_list=[],
            lineno=-1,
        )
        return import_string + "\n" + ast.unparse(import_stmts) + "\n" + ast.unparse(function_ast)
    except Exception:
        return code


def call_method(method, inputs):
    if isinstance(inputs, list):
        inputs = "\n".join(inputs)
    inputs_line_iterator = iter(inputs.split("\n"))
    mock_stdin = MockStdinWithBuffer(inputs)

    @patch("builtins.open", mock_open(read_data=inputs))
    @patch("sys.stdin", mock_stdin)
    @patch("sys.stdin.readline", lambda *args: next(inputs_line_iterator))
    @patch("sys.stdin.readlines", lambda *args: inputs.split("\n"))
    @patch("sys.stdin.read", lambda *args: inputs)
    def _inner_call_method(_method):
        try:
            return _method()
        except SystemExit:
            pass

    return _inner_call_method(method)


def get_function(compiled_sol, fn_name: str):
    try:
        assert hasattr(compiled_sol, fn_name)
        return getattr(compiled_sol, fn_name)
    except Exception:
        return


def compile_code(code: str):
    tmp_sol = ModuleType("tmp_sol", "")
    exec(code, tmp_sol.__dict__)
    if "class Solution" in code:
        compiled_sol = tmp_sol.Solution()
    else:
        compiled_sol = tmp_sol
    if compiled_sol is None:
        raise RuntimeError("compile produced nothing")
    return compiled_sol


def convert_line_to_decimals(line: str):
    try:
        decimal_line = [Decimal(elem) for elem in line.split()]
    except Exception:
        return False, []
    return True, decimal_line


def get_stripped_lines(val: str):
    val = val.strip()
    return [val_line.strip() for val_line in val.split("\n")]


def grade_call_based(conn, code, all_inputs, all_outputs, fn_name):
    code = import_string + "\n\n" + code
    try:
        compiled_sol = compile_code(code)
    except BaseException as e:
        conn.send(("done", [-4], {"error": repr(e)[:300], "error_code": -4,
                                   "error_message": "Compile Error"}))
        return
    method = get_function(compiled_sol, fn_name)
    if method is None:
        conn.send(("done", [-4], {"error_code": -4, "error_message": "Function Not Found"}))
        return
    try:
        parsed_inputs = [[json.loads(line) for line in inputs.split("\n")] for inputs in all_inputs]
        parsed_outputs = [json.loads(output) for output in all_outputs]
    except Exception as e:
        conn.send(("done", [-4], {"error": repr(e)[:300], "error_code": -4,
                                  "error_message": "Input Parse Error"}))
        return
    conn.send(("compiled",))
    all_results = []
    for gt_inp, gt_out in zip(parsed_inputs, parsed_outputs):
        try:
            prediction = method(*gt_inp)
        except BaseException as e:
            all_results.append(-4)
            conn.send(("done", all_results, {
                "error": repr(e)[:300], "error_code": -4, "error_message": "Runtime Error",
                "inputs": truncatefn(gt_inp), "expected": truncatefn(gt_out)}))
            return
        if isinstance(prediction, tuple):
            prediction = list(prediction)
        if prediction == gt_out:
            all_results.append(True)
            conn.send(("test", True, {}))
            continue
        all_results.append(-2)
        conn.send(("done", all_results, {
            "output": truncatefn(prediction), "inputs": truncatefn(gt_inp),
            "expected": truncatefn(gt_out), "error_code": -2, "error_message": "Wrong Answer"}))
        return
    conn.send(("done", all_results, {"execution": "ok"}))


def grade_stdio(conn, code, all_inputs, all_outputs):
    code = clean_if_name(code)
    code = make_function(code)
    try:
        compiled_sol = compile_code(code)
    except BaseException as e:
        conn.send(("done", [-4], {"error": repr(e)[:300], "error_code": -4,
                                  "error_message": "Compile Error"}))
        return
    method = get_function(compiled_sol, "wrapped_function")
    if method is None:
        conn.send(("done", [-4], {"error_code": -4, "error_message": "No runnable code found"}))
        return
    conn.send(("compiled",))
    all_results = []
    for gt_inp, gt_out in zip(all_inputs, all_outputs):
        try:
            with Capturing() as captured_output:
                call_method(method, gt_inp)
        except BaseException as e:
            all_results.append(-4)
            conn.send(("done", all_results, {
                "error": repr(e)[:300], "error_code": -4, "error_message": "Runtime Error",
                "inputs": truncatefn(gt_inp), "expected": truncatefn(gt_out)}))
            return
        prediction = captured_output[0]
        stripped_prediction_lines = get_stripped_lines(prediction)
        stripped_gt_out_lines = get_stripped_lines(gt_out)
        if len(stripped_prediction_lines) != len(stripped_gt_out_lines):
            all_results.append(-2)
            conn.send(("done", all_results, {
                "output": truncatefn(prediction), "inputs": truncatefn(gt_inp),
                "expected": truncatefn(gt_out), "error_code": -2,
                "error_message": "Wrong answer: mismatched output length"}))
            return
        wrong = False
        for output_line_idx, (pl, gl) in enumerate(
            zip(stripped_prediction_lines, stripped_gt_out_lines)
        ):
            if pl == gl:
                continue
            ok1, dp = convert_line_to_decimals(pl)
            ok2, dg = convert_line_to_decimals(gl)
            if ok1 and ok2 and dp == dg:
                continue
            all_results.append(-2)
            conn.send(("done", all_results, {
                "output": truncatefn(prediction), "inputs": truncatefn(gt_inp),
                "expected": truncatefn(gt_out), "error_code": -2,
                "error_message": f"Wrong answer at line {output_line_idx}"}))
            wrong = True
            break
        if wrong:
            return
        all_results.append(True)
        conn.send(("test", True, {}))
    conn.send(("done", all_results, {"execution": "ok"}))


def _eval_child(conn):
    """Child process: evaluate ONE generated code against one problem's tests,
    streaming results to the parent (which enforces the timeout watchdog)."""
    try:
        try:
            sys.set_int_max_str_digits(1_000_000)
        except AttributeError:
            pass
        code, in_outs, _timeout = conn.recv()
        if not code or not code.strip():
            conn.send(("done", [-4], {"error_code": -4, "error_message": "Empty code"}))
            return
        fn_name = in_outs.get("fn_name")
        if fn_name is None:
            grade_stdio(conn, code, in_outs["inputs"], in_outs["outputs"])
        else:
            grade_call_based(conn, code, in_outs["inputs"], in_outs["outputs"], fn_name)
    except BaseException as e:
        try:
            conn.send(("fatal", repr(e)[:500]))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _eval_one_generation(code, in_outs, timeout):
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe()
    proc = ctx.Process(target=_eval_child, args=(child_conn,), daemon=True)
    proc.start()
    parent_conn.send((code, in_outs, timeout))

    results, meta = [-4], {"error_code": -4, "error_message": "Child produced nothing"}
    partial = None
    # generous first budget: windows process startup + payload size
    budget = timeout + 20.0
    got_any = False
    try:
        while True:
            if not parent_conn.poll(budget):
                # watchdog
                results = (partial if partial else []) + [-3]
                meta = {"error_code": -3, "error_message": "Time Limit Exceeded (watchdog)"}
                break
            msg = parent_conn.recv()
            got_any = True
            budget = timeout + 3.0
            if msg[0] == "compiled":
                continue
            if msg[0] == "test":
                partial = (partial or []) + [msg[1]]
                continue
            if msg[0] == "done":
                results, meta = msg[1], msg[2]
                break
            if msg[0] == "fatal":
                results = (partial if partial else []) + [-4]
                meta = {"error_code": -4, "error_message": msg[1]}
                break
    except EOFError:
        results = (partial if partial else [-4])
        meta = meta if got_any and results != [-4] else {
            "error_code": -4, "error_message": "Child process died"}
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=10)
    return results, meta


def eval_one_problem(job):
    qid = job[0]
    in_out_str, codes, timeout = job[1], job[2], job[3]
    scenario = job[4] if len(job) > 4 else "codegen"
    if scenario == "exec":
        return eval_exec_problem(qid, in_out_str, codes, timeout)
    if scenario == "top":
        return eval_top_problem(qid, in_out_str, codes)
    in_outs = json.loads(in_out_str)
    results, metas = [], []
    for code in codes:
        res, meta = _eval_one_generation(code, in_outs, timeout)
        results.append(res)
        metas.append(meta)
    return {"qid": qid, "results": results, "meta": metas}


# ---- code_execution scenario scoring (port of compute_code_execution_metrics)
# Official: exec BASE_IMPORTS + problem code + "assert {output} == {prediction}"
# in a sandbox; True iff no exception. Predictions whose text contains the raw
# input string are skipped (and excluded from n) exactly like the official code.

EXEC_BASE_IMPORTS = (
    "from itertools import accumulate, chain, combinations, count, permutations, "
    "product, groupby, islice, repeat\n"
    "from copy import deepcopy\nfrom string import ascii_lowercase\n"
    "from math import floor, log2, log10, sqrt, comb, gcd, ceil, inf, isqrt\n"
    "from collections import defaultdict, deque, Counter\n"
    "from bisect import bisect, bisect_left, bisect_right, insort\n"
    "from heapq import heappush, heappop, heapify, merge\n"
    "from functools import reduce, cache, lru_cache\n"
    "from random import randrange, shuffle\nfrom operator import itemgetter, sub\n"
    "from re import search as re_search\nfrom os.path import commonprefix\n"
    "from typing import List, Tuple, Dict, Set, Optional, Union, Any, Callable, "
    "Iterable, Iterator, Generator\nimport copy\nimport string\nimport math\n"
    "import collections\nimport bisect\nimport heapq\nimport functools\n"
    "import random\nimport itertools\nimport operator\nimport re\nimport numpy as np\n"
    "import pandas as pd\nfrom math import log, prod\nfrom collections import deque, "
    "defaultdict, Counter, OrderedDict\nfrom itertools import accumulate, permutations, "
    "combinations, product, groupby, islice, chain, repeat, zip_longest, cycle\n"
    "from functools import lru_cache, reduce, partial\nfrom operator import iand\n"
    "import sys\n"
)


def _exec_child(conn):
    try:
        try:
            sys.set_int_max_str_digits(1_000_000)
        except AttributeError:
            pass
        code, out_literal, answer = conn.recv()[:3]
        g = {}
        exec(EXEC_BASE_IMPORTS, g)
        conn.send(("ready", True))
        exec(code + "\n", g)
        conn.send(("compiled", True))
        try:
            ok = bool(eval(f"({out_literal}) == ({answer})", g))
        except BaseException:
            ok = False
        conn.send(("done", ok))
    except BaseException as e:
        try:
            conn.send(("failed", repr(e)[:300]))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _eval_exec_sample(inp, code, out_literal, answer, timeout):
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe()
    proc = ctx.Process(target=_exec_child, args=(child_conn,), daemon=True)
    proc.start()
    parent_conn.send((code, out_literal, answer))
    ok, meta = False, {"error_code": -4, "error_message": "Exec child failed"}
    # first budget covers Windows spawn + numpy/pandas imports (measured, not timed)
    stage_budgets = [("ready", 240.0), ("compiled", timeout + 2.0),
                     ("done", timeout + 2.0)]
    try:
        for want, budget in stage_budgets:
            if not parent_conn.poll(budget):
                meta = {"error_code": -3, "error_message": "Time Limit Exceeded (watchdog)"}
                ok = False
                return ok, meta
            msg = parent_conn.recv()
            if msg[0] == want:
                if want == "done":
                    ok, meta = msg[1], {"execution": "ok"}
                continue
            if msg[0] == "failed":
                meta = {"error_code": -4, "error_message": msg[1]}
                return ok, meta
    except EOFError:
        meta = {"error_code": -4, "error_message": "Exec child died"}
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=10)
    return ok, meta


def eval_exec_problem(qid, payload_str, answers, timeout):
    d = json.loads(payload_str)
    results, skipped, metas = [], [], []
    for answer in answers:
        if d["input"] and d["input"] in answer:
            # official quirk: skip (exclude from n) answers echoing the input
            results.append([False])
            skipped.append(True)
            metas.append({"skipped": "input echoed in answer"})
            continue
        ok, meta = _eval_exec_sample(d["input"], d["code"], d["output"], answer, timeout)
        results.append([bool(ok)])
        skipped.append(False)
        metas.append(meta)
    return {"qid": qid, "kind": "exec", "results": results,
            "skipped": skipped, "meta": metas}


# ---- test_output_prediction scenario scoring (port of
# compute_test_output_prediction_metrics; eval restricted to literals)

def _top_parse_assert(statement):
    try:
        parsed = ast.parse(statement, mode="exec")
    except SyntaxError:
        return "Invalid syntax"
    if len(parsed.body) == 0:
        return "Empty statement"
    if not isinstance(parsed.body[0], ast.Assert):
        return "Not an assert statement"
    comparison = parsed.body[0].test
    if not isinstance(comparison, ast.Compare) or not isinstance(comparison.ops[0], ast.Eq):
        return "Not an equality assertion"
    return ast.get_source_segment(statement, comparison.comparators[0])


def _check_top(pred, expected):
    if len(pred.splitlines()) > 1:
        for line in pred.splitlines():
            if line.startswith("#"):
                continue
            if "assert" in line:
                pred = line
                break
    pred = pred.strip()
    if "assert" in pred:
        pred_str = str(_top_parse_assert(pred))
    else:
        pred_str = pred
    try:
        pred_val = eval(pred_str, {"__builtins__": {}}, {})
    except Exception:
        return False
    try:
        expected_val = json.loads(expected)
    except Exception:
        return False
    return pred_val == expected_val


def eval_top_problem(qid, payload_str, preds):
    expected = json.loads(payload_str)["expected"]
    results = [[bool(_check_top(p, expected))] for p in preds]
    return {"qid": qid, "kind": "top", "results": results, "meta": [{}, ] * len(preds)}


# -----------------------------------------------------------------------------
# Metrics (official pass@k formula from lcb_runner pass_k_utils)
# -----------------------------------------------------------------------------


def estimate_pass_at_k(n, c, k):
    if n - c < k:
        return 1.0
    import numpy as np

    return float(1.0 - np.prod(1.0 - k / np.arange(n - c + 1, n + 1)))


def compute_pass_metrics(per_problem):
    out = {}
    if not per_problem:
        return out
    for k in (1, 5, 10):
        if all(n >= k for n, _ in per_problem):
            vals = [estimate_pass_at_k(n, c, k) for n, c in per_problem]
            out[f"pass@{k}"] = sum(vals) / len(vals)
    return out


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


def slugify(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "model"


def harness_detected():
    """"yes" if this process was started from inside the deepseek-harness agent
    (its shells export DSH_* vars), else "no". Recorded in the summary and shown
    in the report, because a harness chat hitting the same -np 1 server queues
    with the benchmark and inflates wall time. Override with --harness yes|no."""
    return "yes" if any(os.environ.get(v) for v in ("DSH_SESSION_ID", "DSH_SHELL")) else "no"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--name", help="Short name for this model run (folder + report row)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1",
                    help="OpenAI-compatible base URL (LM Studio: http://localhost:1234/v1)")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--model", default=None, help="Server model id (default: first from /v1/models)")
    ap.add_argument("--n", type=int, default=1, help="Samples per problem")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--max-tokens", type=int, default=16384)
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
                    help="Stratified random subset of N problems (fair + fast; same subset "
                         "every time via fixed seed, saved to bench/<name>/sample_ids.json "
                         "so every model is scored on the identical problems)")
    ap.add_argument("--extractor", default="auto", choices=["auto", "official"],
                    help="auto = official last-fence rule + truncation/fenceless fallback")
    ap.add_argument("--timeout", type=int, default=6,
                    help="Eval time budget per test case, in seconds (official: 6)")
    ap.add_argument("--eval-workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--skip-generate", action="store_true")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--speed-probe", action="store_true",
                    help="Measure server prefill ('lecture') + decode tok/s with 2 probe requests")
    ap.add_argument("--probe-only", action="store_true",
                    help="With --speed-probe: measure tok/s and exit (no datasets, no "
                         "generation). The saved probe is reused by the real run.")
    ap.add_argument("--scenario", default="code_generation",
                    choices=["code_generation", "codegen", "code_execution", "exec",
                             "test_output_prediction", "top"],
                    help="Benchmark scenario. code_generation (default) is the main "
                         "leaderboard one; code_execution simulates code output "
                         "(livecodebench/execution-v2), test_output_prediction "
                         "predicts the test answer (livecodebench/test_generation).")
    ap.add_argument("--exec-cot", action="store_true",
                    help="Step-by-step examples for code_execution "
                         "(official --cot_code_execution); default is the direct prompt")
    ap.add_argument("--self-test", action="store_true",
                    help="Sanity-check the Windows evaluator and exit (no server needed)")
    ap.add_argument("--harness", default=None, choices=["yes", "no"],
                    help="Record in the summary/report whether this run was made while "
                         "the deepseek-harness chat (or any agent) was open on the same "
                         "model server. A shared -np 1 server queues the two, which "
                         "inflates wall time - check this column before comparing rows. "
                         "Default: auto-detected from DSH_* env vars.")
    ap.add_argument("--set-harness", default=None, choices=["yes", "no"],
                    metavar="{yes,no}",
                    help="With --name: backfill the harness column of an existing run "
                         "in its _summary.json and rebuild the report (no server, "
                         "no regeneration)")
    args = ap.parse_args()

    scen = {"code_generation": "codegen", "codegen": "codegen",
            "code_execution": "exec", "exec": "exec",
            "test_output_prediction": "top", "top": "top"}[args.scenario]

    if args.self_test:
        sys.exit(self_test())

    os.makedirs("bench", exist_ok=True)
    if args.report_only:
        build_report()
        return
    if args.set_harness:
        if not args.name:
            ap.error("--set-harness needs --name of an existing run, e.g. "
                     "--set-harness yes --name qwen38-flash-next-q2")
        sp = os.path.join("bench", slugify(args.name), "_summary.json")
        if not os.path.isfile(sp):
            sys.exit(f"No summary for '{args.name}' at {sp} - nothing to annotate.")
        with open(sp, encoding="utf-8") as f:
            data = json.load(f)
        data["harness"] = args.set_harness
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"{args.name}: harness = {args.set_harness}")
        build_report()
        return
    if not args.name:
        ap.error("--name is required (unless --report-only): pick a short name for this "
                 "model, e.g. --name qwen3-coder-next-iq4xs. Everything is stored in "
                 "bench/<name>/, so re-running the same command resumes that run.")

    slug = slugify(args.name)
    run_dir = os.path.join("bench", slug)
    os.makedirs(run_dir, exist_ok=True)

    try:
        server_models = requests.get(f"{args.base_url}/models", timeout=10).json()
        model_id = args.model or server_models["data"][0]["id"]
        print(f"Server model: {model_id}")
    except Exception:
        model_id = args.model or args.name
        print(f"Could not query {args.base_url}/models; using '{model_id}' as model id")

    if args.speed_probe:
        sp_path = os.path.join(run_dir, "speed_probe.json")
        if os.path.exists(sp_path):
            print(f"Speed probe already recorded: {open(sp_path, encoding='utf-8').read().strip()}")
        else:
            print("Running server speed probe (may wait behind a generation)...")
            probe = speed_probe(args.base_url, model_id)
            probe["model"] = model_id
            with open(sp_path, "w", encoding="utf-8") as f:
                json.dump(probe, f, indent=2)
            print(f"Speed probe: {probe}")

    if args.probe_only:
        if not args.speed_probe:
            ap.error("--probe-only requires --speed-probe")
        print("--probe-only: stopping here, nothing generated. Use the same --name for "
              "the real run; the saved probe is reused (bench/<name>/speed_probe.json).")
        return

    sample_file = os.path.join(run_dir, "sample_ids.json")
    if scen == "codegen":
        problems = load_problems(args.release, args.start_date, args.end_date,
                                 args.difficulty, args.limit,
                                 args.random_sample, sample_file)
    elif scen == "exec":
        problems = load_exec_problems(args.release, args.start_date, args.end_date,
                                      args.difficulty, args.limit,
                                      args.random_sample, sample_file,
                                      cot=args.exec_cot)
    else:
        problems = load_top_problems(args.release, args.start_date, args.end_date,
                                     args.difficulty, args.limit,
                                     args.random_sample, sample_file)
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
        elif scen == "exec":
            codes = [extract_exec_answer(o, args.exec_cot) for o in outs]
        else:
            codes = [extract_top_answer(o) for o in outs]
        generations.append({"question_id": qid, "output_list": outs, "code_list": codes})
        compat.append({"question_id": qid, "code_list": codes})
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
            jobs.append((qid, probs_by_id[qid].get("eval_payload", probs_by_id[qid]["in_out"]),
                         entry["code_list"], args.timeout, scen))

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
    else:
        print("Evaluation already complete for current problem set")

    # ---- Phase 4: metrics ------------------------------------------------------
    per_problem, per_diff = [], {}
    per_problem_pass = {}
    for qid in qorder:
        if qid not in gen_map:
            continue  # stale verdict for a problem with no (kept) generation
        rec = evaluated.get(qid)
        if not rec:
            continue
        kind = rec.get("kind", "codegen")
        if kind == "codegen":
            n = len(rec["results"])
            c = sum(1 for res in rec["results"] if res and all(x > 0 for x in res))
        elif kind == "exec":
            # official: skipped (input-echoing) generations are excluded from n;
            # if everything was skipped, n = len(results) and c = 0
            skips = rec.get("skipped", [False] * len(rec["results"]))
            scored = [r for r, s in zip(rec["results"], skips) if not s]
            c = sum(1 for r in scored if r and all(bool(x) for x in r))
            n = len(scored) if scored else len(rec["results"])
        else:  # top
            n = len(rec["results"])
            c = sum(1 for r in rec["results"] if r and all(bool(x) for x in r))
        per_problem.append((n, c))
        per_problem_pass[qid] = c / n if n else 0.0
        diff = probs_by_id[qid]["difficulty"].lower()
        per_diff.setdefault(diff, []).append((n, c))

    metrics = compute_pass_metrics(per_problem)
    diff_metrics = {d: compute_pass_metrics(v) for d, v in sorted(per_diff.items())}

    gen_secs_total = sum(r["seconds"] for r in done.values() if r.get("seconds"))
    gen_tok_total = sum(r["completion_tokens"] or 0 for r in done.values())
    prompt_tok_total = sum(r.get("prompt_tokens") or 0 for r in done.values())
    n_gen = len(done)
    truncated = sum(
        1 for r in done.values()
        if r.get("completion_tokens")
        and r["completion_tokens"] >= (r.get("max_tokens") or args.max_tokens or 0)
    )
    trunc_qids = {
        (k[0] if isinstance(k, tuple) else k)
        for k, r in done.items()
        if r.get("completion_tokens")
        and r["completion_tokens"] >= (r.get("max_tokens") or args.max_tokens or 0)
    }
    empty_outputs = sum(1 for r in done.values() if not r.get("output"))
    # Pass rate among problems whose answer was NOT cut off at the token cap:
    # separates "can it solve the problem" from "did it fit in the budget".
    complete_rates = [v for q, v in per_problem_pass.items() if q not in trunc_qids]
    failed_requests = 0
    if os.path.exists(gen_path):
        with open(gen_path, encoding="utf-8") as f:
            failed_requests = sum(1 for line in f if line.strip()) - n_gen
    # Server-side timing aggregates (llama.cpp excludes queue wait): exact speed
    pn = sum((r.get("timings") or {}).get("prompt_n") or 0 for r in done.values())
    pms = sum((r.get("timings") or {}).get("prompt_ms") or 0 for r in done.values())
    pnn = sum((r.get("timings") or {}).get("predicted_n") or 0 for r in done.values())
    pnm = sum((r.get("timings") or {}).get("predicted_ms") or 0 for r in done.values())
    probe_path = os.path.join(run_dir, "speed_probe.json")
    probe = json.load(open(probe_path, encoding="utf-8")) if os.path.exists(probe_path) else {}
    prefill_tps = round(pn * 1000.0 / pms, 1) if pms and pn else probe.get("prefill_tokens_per_sec")
    decode_tps = round(pnn * 1000.0 / pnm, 1) if pnm and pnn else probe.get("decode_tokens_per_sec")
    if decode_tps is None:
        decode_tps = round(gen_tok_total / gen_secs_total, 1) if gen_secs_total else None
    # harness provenance: explicit flag wins, then whatever an earlier phase of
    # this same run already recorded (a later --skip-generate re-score runs from
    # a different shell and must not silently flip it), then env auto-detection.
    summary_path = os.path.join(run_dir, "_summary.json")
    prev_harness = None
    if os.path.exists(summary_path):
        try:
            with open(summary_path, encoding="utf-8") as f:
                prev_harness = json.load(f).get("harness")
        except Exception:
            prev_harness = None
    harness_val = args.harness or prev_harness or harness_detected()
    summary = {
        "name": args.name,
        "server_model": model_id,
        "harness": harness_val,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "config": {
            "n": args.n, "temperature": args.temperature, "top_p": args.top_p,
            "max_tokens": args.max_tokens, "release": args.release,
            "start_date": args.start_date, "end_date": args.end_date,
            "difficulty": args.difficulty, "limit": args.limit or None,
            "random_sample": args.random_sample or None,
            "extractor": args.extractor, "eval_timeout": args.timeout,
            "scenario": scen, "exec_cot": bool(args.exec_cot),
            "problems_total": len(per_problem),
        },
        "sample_hash": hashlib.sha1(",".join(str(q) for q in sorted(
            str(p["question_id"]) for p in problems)).encode()).hexdigest()[:12],
        "counts": {d: len(v) for d, v in sorted(per_diff.items())},
        "generation_stats": {
            "generated": n_gen,
            "failed_requests": failed_requests,
            "not_generated": len(missing),
            "truncated_at_max_tokens": truncated,
            "truncated_pct": round(100.0 * truncated / n_gen, 1) if n_gen else None,
            "problems_not_truncated": len(complete_rates),
            "pass_rate_when_not_truncated": round(
                100.0 * sum(complete_rates) / len(complete_rates), 1)
            if complete_rates else None,
            "empty_outputs": empty_outputs,
        },
        # SINGLE HEADLINE VALUE: pass@1 % on the sampled problem set. Only runs
        # with the same problem count / sample_hash are directly comparable.
        "score": round(metrics.get("pass@1", 0.0) * 100, 2) if metrics.get("pass@1") is not None else None,
        "metrics": metrics,
        "metrics_by_difficulty": diff_metrics,
        "speed": {
            "total_generation_seconds": round(gen_secs_total, 1),
            "total_completion_tokens": gen_tok_total,
            "mean_tokens_per_sec": round(gen_tok_total / gen_secs_total, 2)
            if gen_secs_total else None,
            "mean_seconds_per_problem": round(gen_secs_total / len(done), 1)
            if done else None,
            "avg_prompt_tokens": round(prompt_tok_total / len(done), 0)
            if (done and prompt_tok_total) else None,
            "avg_completion_tokens": round(gen_tok_total / len(done), 0) if done else None,
            "prefill_tokens_per_sec": prefill_tps,
            "decode_tokens_per_sec": decode_tps,
            "speed_probe": probe or None,
        },
        "per_problem_pass": per_problem_pass,
    }
    if per_problem:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
    else:
        print("No evaluated problems; summary not written (probe saved if run).")

    print("\n=== Results ===")
    if not per_problem:
        print("Nothing evaluated yet.")
    if summary.get("score") is not None:
        print(f"SCORE: {summary['score']}%  "
              f"({len(per_problem)} problems, {args.n} sample(s) each) "
              f"[sample {summary['sample_hash']}]")
    for k, v in metrics.items():
        print(f"{k:>8}: {v * 100:.2f}%")
    for d, mm in diff_metrics.items():
        if "pass@1" in mm:
            print(f"  {d:>7}: {mm['pass@1'] * 100:.2f}%  ({len(per_diff[d])} problems)")
    spd = summary["speed"]
    if spd.get("decode_tokens_per_sec"):
        print(f"   speed: decode {spd['decode_tokens_per_sec']} tok/s, "
              f"prefill/lecture {spd['prefill_tokens_per_sec']} tok/s")
    if spd.get("mean_seconds_per_problem"):
        avg_p = spd.get("avg_prompt_tokens")
        avg_txt = f"avg {avg_p} prompt + " if avg_p else "avg ? prompt + "
        print(f"         {avg_txt}{spd['avg_completion_tokens']} completion tokens per problem, "
              f"{spd['mean_seconds_per_problem']}s/problem wall time")
    gs = summary.get("generation_stats") or {}
    if gs.get("truncated_at_max_tokens") or gs.get("failed_requests"):
        print(f"   gen: {gs.get('truncated_at_max_tokens', 0)}/{gs.get('generated', 0)} "
              f"hit the {args.max_tokens}-token cap (truncated = auto-fail), "
              f"{gs.get('failed_requests', 0)} failed request(s) "
              f"(rerun the same command to retry them)")
    if gs.get("pass_rate_when_not_truncated") is not None:
        print(f"      when-complete: {gs['pass_rate_when_not_truncated']}% of the "
              f"{gs.get('problems_not_truncated')}/{gs.get('generated')} answers that fit "
              f"in the token budget passed (skill signal, budget aside)")
    if args.harness:
        hn = ""
    elif prev_harness:
        hn = " (kept from an earlier phase of this run; --harness yes|no to change)"
    else:
        hn = " (auto-detected from DSH_* env vars; --harness yes|no to override)"
    print(f"   harness: {harness_val}{hn}")
    print(f"Summary: {summary_path}\n")

    build_report()


# -----------------------------------------------------------------------------
# Cross-model report
# -----------------------------------------------------------------------------


def build_report():
    rows = []
    if os.path.isdir("bench"):
        for entry in sorted(os.listdir("bench")):
            sp = os.path.join("bench", entry, "_summary.json")
            if os.path.isfile(sp):
                with open(sp, encoding="utf-8") as f:
                    rows.append(json.load(f))
    if not rows:
        print("No summaries found under bench/ yet.")
        return
    rows.sort(key=lambda r: (r.get("config", {}).get("scenario", ""),
                             -(r.get("score") or 0.0)))
    headers = ["model", "harness", "scenario", "SCORE", "problems", "n", "cap",
               "trunc%", "when-complete", "easy", "medium", "hard", "prefill-tok/s",
               "gen-tok/s", "gen-min", "sample", "date"]
    table = []
    for r in rows:
        m = r.get("metrics", {})
        dm = r.get("metrics_by_difficulty", {})
        sp = r.get("speed", {})
        cfg = r.get("config", {})
        gs = r.get("generation_stats", {})

        def fmt_pct(x):
            return f"{x * 100:.1f}" if isinstance(x, (int, float)) else "-"

        total = sum(r.get("counts", {}).values())
        decode = sp.get("decode_tokens_per_sec") or sp.get("mean_tokens_per_sec")
        table.append([
            r["name"], r.get("harness") or "unknown", cfg.get("scenario", "codegen"),
            r.get("score"), total, cfg.get("n"),
            cfg.get("max_tokens"),
            gs.get("truncated_pct"),
            gs.get("pass_rate_when_not_truncated", "-") if gs.get("pass_rate_when_not_truncated") is not None else "-",
            fmt_pct(dm.get("easy", {}).get("pass@1")),
            fmt_pct(dm.get("medium", {}).get("pass@1")),
            fmt_pct(dm.get("hard", {}).get("pass@1")),
            sp.get("prefill_tokens_per_sec"),
            decode,
            round((sp.get("total_generation_seconds") or 0) / 60, 1),
            r.get("sample_hash") or "-",
            (r.get("generated_at") or "")[:10],
        ])
    widths = [max(len(str(h)), *(len(str(row[i])) for row in table))
              for i, h in enumerate(headers)]
    header_line = "  ".join(f"{h:<{w}}" for h, w in zip(headers, widths))
    out = ["\n" + header_line, "-" * len(header_line)]
    for row in table:
        out.append("  ".join(f"{str(v):<{w}}" for v, w in zip(row, widths)))
    text = "\n".join(out)
    print("LiveCodeBench comparison:")
    print(text)
    print("\nSCORE = pass@1 % (the single number: higher is better). when-complete ="
          "\npass rate over answers that did not hit the token cap. harness = was the"
          "\ndeepseek-harness/agent chat open on the same server for that run (its"
          "\nrequests queue behind the benchmark, inflating gen-min); unknown = run"
          "\nfrom before this column existed, set it with --set-harness. Rows are"
          "\ndirectly comparable only when scenario / problems / n / cap / sample"
          "\ncolumns match.")
    os.makedirs("bench", exist_ok=True)
    with open("bench/report.md", "w", encoding="utf-8") as f:
        f.write("# LiveCodeBench model comparison\n\n```\n" + text + "\n```\n\n"
                "SCORE = pass@1 % on the sampled problem set - the single headline value.\n"
                "prefill-tok/s = prompt-reading (lecture) speed; gen-tok/s = generation speed.\n"
                "cap = per-answer max_tokens (thinking budget); trunc% = share of answers that hit it.\n"
                "when-complete = pass rate over the answers that did NOT hit the cap (skill signal).\n"
                "harness = was the deepseek-harness/agent chat open on the same model server\n"
                "during that run: its requests queue behind the benchmark, so gen-min (wall time)\n"
                "of harness=yes rows is inflated; unknown = run predates this column, backfill\n"
                "it with --set-harness yes|no --name <run>.\n"
                "Rows are directly comparable only when scenario / problems / n / cap /\n"
                "sample columns match.\n")
    with open("bench/report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(table)
    print("Report saved: bench/report.md, bench/report.csv")


# -----------------------------------------------------------------------------
# Self test (no model server needed)
# -----------------------------------------------------------------------------


def self_test():
    timeout = 2
    cases = []

    def case(name, in_out, codes, expect):
        cases.append((name, in_out, codes, expect))

    stdio_in_out = json.dumps({
        "inputs": ["3 4\n", "10 20\n", "1 1\n"],
        "outputs": ["7", "30", "2"],
        "fn_name": None,
    })
    call_in_out = json.dumps({
        # call-based format: each input string is ONE test case; each LINE of it
        # is one JSON-encoded argument passed to fn_name(*args).
        "inputs": ["1\n2", "3\n4"],
        "outputs": ["3", "7"],
        "fn_name": "plus",
    })
    case("stdio good", stdio_in_out,
         ["import sys\ndata = sys.stdin.read().split()\nprint(int(data[0]) + int(data[1]))\n"], [True])
    case("stdio wrong", stdio_in_out, ["print(42)\n"], [False])
    case("stdio infinite loop (watchdog)", stdio_in_out, ["while True:\n    pass\n"], [False])
    case("stdio runtime error", stdio_in_out, ["raise ValueError('boom')\n"], [False])
    case("call good", call_in_out, ["def plus(a, b):\n    return a + b\n"], [True])
    case("call wrong", call_in_out, ["def plus(a, b):\n    return a - b\n"], [False])
    case("call missing function", call_in_out, ["print('hi')\n"], [False])
    case("mixed multi-sample", stdio_in_out, [
        "import sys\ndata = sys.stdin.read().split()\nprint(int(data[0]) + int(data[1]))\n",
        "print(42)\n",
        "while True:\n    pass\n",
    ], [True, False, False])

    ok = True
    for name, in_out, codes, expected in cases:
        t0 = time.time()
        rec = eval_one_problem(("selftest", in_out, codes, timeout))
        flags = [bool(r) and all(x > 0 for x in r) for r in rec["results"]]
        status = "OK " if flags == expected else "FAIL"
        if flags != expected:
            ok = False
        print(f"[{status}] {name}: got {flags} want {expected} "
              f"({time.time() - t0:.1f}s)")
        for m in rec["meta"]:
            print(f"      meta: code={m.get('error_code')} msg={m.get('error_message')!r}")
    # --- scenario checks: code_execution + test_output_prediction -----------
    exec_payload = json.dumps(
        {"code": "def f(x):\n    return x * 2\n", "input": "f(3)", "output": "6"})
    t0 = time.time()
    rec = eval_one_problem(("selftest", exec_payload, ["6", "7", "f(3)"], timeout, "exec"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, False]
    sok = flags == want and rec.get("skipped") == [False, False, True]
    ok = ok and sok
    print(f"[{'OK ' if sok else 'FAIL'}] exec scenario (numpy/pandas warm child): "
          f"got {flags} skipped={rec.get('skipped')} want {want} "
          f"({time.time() - t0:.1f}s)")

    top_payload = json.dumps({"expected": '"ab"'})
    preds = [
        'assert func("a") == "ab"',
        'assert func("a") == "a"',
        "Here you go:\n```python\nassert func('a') == 'ab'\n```",
    ]
    rec = eval_one_problem(("selftest", top_payload, preds, timeout, "top"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, True]
    tok_ok = flags == want
    ok = ok and tok_ok
    print(f"[{'OK ' if tok_ok else 'FAIL'}] top scenario: got {flags} want {want}")

    checks = [
        ("exec-extract direct",
         extract_exec_answer('assert f(3) == 6\ntrailing noise', cot=False), "6"),
        ("exec-extract cot",
         extract_exec_answer('thinking...\n[ANSWER]\nassert f(3) == 42\n[/ANSWER]', cot=True), "42"),
        ("top-extract assert line",
         extract_top_answer('blah\nassert func(1) == 2\ntrailing'), "assert func(1) == 2"),
        ("top-extract fence",
         extract_top_answer('Here:\n```python\nassert func(1) == 2\n```'), "assert func(1) == 2"),
    ]
    for name, got, want in checks:
        eok = got == want
        ok = ok and eok
        print(f"[{'OK ' if eok else 'FAIL'}] {name}: got {got!r} want {want!r}")

    print("\nSelf-test:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
