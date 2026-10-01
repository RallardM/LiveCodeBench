"""The three LiveCodeBench pools: code generation, code execution and test
output prediction (the last two can be bundled)."""

import base64
import datetime as dt
import json
import pickle
import zlib

from . import state
from .bundling import (bundle_capacity, bundle_exec_cot, bundle_exec_problems,
                       bundle_top_problems)
from .prompts import format_prompt, format_prompt_exec, format_prompt_top
from .sampling import filter_sample

DATASET_NAME = "livecodebench/code_generation_lite"
EXEC_DATASET_NAME = "livecodebench/execution-v2"
TOP_DATASET_NAME = "livecodebench/test_generation"


def load_problems(release, start_date, end_date, difficulty, limit,
                  random_sample=0, sample_ids_file=None, hardest=0, mix=None):
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
        random_sample, sample_ids_file, hardest=hardest, mix=mix,
    )


def load_exec_problems(release, start_date, end_date, difficulty, limit,
                       random_sample=0, sample_ids_file=None, cot=False,
                       hardest=0, mix=None, bundle=1):
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
                # weight: how much code one call carries.
                "weight": (len(row["code"] or "")
                           + len(str(row["output"] or ""))),
                "messages": format_prompt_exec(row["code"], row["input"], cot),
            }
        )
    if bundle and bundle > 1:
        caps = bundle_capacity(problems)
        raw_n = len(problems)
        raw_q = len({str(p["question_id"]).split("#")[0] for p in problems})
        if cot:
            packed = bundle_exec_problems(problems, bundle)
            problems = bundle_exec_cot(problems, bundle, packed)
        else:
            problems = bundle_exec_problems(problems, bundle)
        problems.sort(key=lambda p: str(p["question_id"]))
        out = filter_sample(
            problems, start_date, end_date, difficulty, limit,
            random_sample, sample_ids_file, hardest=hardest, mix=mix,
        )
        state.POOL_FACTS["bundle_cap"] = caps
        state.POOL_FACTS["bundle_k"] = bundle
        state.POOL_FACTS["raw_rows"] = raw_n
        state.POOL_FACTS["raw_questions"] = raw_q
        return out
    return filter_sample(
        problems, start_date, end_date, difficulty, limit,
        random_sample, sample_ids_file, hardest=hardest, mix=mix,
    )


def load_top_problems(release, start_date, end_date, difficulty, limit,
                      random_sample=0, sample_ids_file=None, hardest=0, mix=None,
                      bundle=1):
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
                # weight: how much problem text the predicted output comes from.
                "weight": (len(row["question_content"] or "")
                           + len(row["starter_code"] or "")
                           + len(str(test["output"] or ""))),
                "messages": format_prompt_top(
                    row["question_content"], row["starter_code"],
                    row["function_name"], test["input"]
                ),
                # kept for --bundle: rebuild a multi-test prompt of the same problem
                "q_content": row["question_content"],
                "starter": row["starter_code"],
                "fname": row["function_name"],
                "test_input": test["input"],
            }
        )
    if bundle and bundle > 1:
        caps = bundle_capacity(problems)
        raw_n = len(problems)
        raw_q = len({str(p["question_id"]).split("#")[0] for p in problems})
        problems = bundle_top_problems(problems, bundle)
        problems.sort(key=lambda p: str(p["question_id"]))
        out = filter_sample(
            problems, start_date, end_date, difficulty, limit,
            random_sample, sample_ids_file, hardest=hardest, mix=mix,
        )
        state.POOL_FACTS["bundle_cap"] = caps
        state.POOL_FACTS["bundle_k"] = bundle
        state.POOL_FACTS["raw_rows"] = raw_n
        state.POOL_FACTS["raw_questions"] = raw_q
        return out
    return filter_sample(
        problems, start_date, end_date, difficulty, limit,
        random_sample, sample_ids_file, hardest=hardest, mix=mix,
    )
