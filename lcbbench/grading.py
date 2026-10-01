"""The grader. Windows-safe port of lcb_runner testing_util semantics: every
generated program runs in its own spawned child process and the parent
supervises it with a wall-clock budget (there is no SIGALRM on Windows).
Keep this module light: spawned children import it."""

import ast
import json
import multiprocessing
import sys
import time
from decimal import Decimal
from io import StringIO
from types import ModuleType
from unittest.mock import mock_open, patch

try:  # some LCB test cases contain very large integers
    sys.set_int_max_str_digits(1_000_000)
except AttributeError:  # python < 3.11
    pass

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


# ---- --bundle scoring: K sub-answers per generation, the item counts as
# passed only when every sub-answer is right (all or nothing). The sub-results
# ride along in "sub" so the score line can print the per-call rate too.

def eval_exec_bundle_problem(qid, payload_str, answers, timeout):
    items = json.loads(payload_str)["items"]
    results, skipped, metas, subs = [], [], [], []
    for answer in answers:
        ans = list(answer) if isinstance(answer, (list, tuple)) else [answer]
        ans = ans + [""] * (len(items) - len(ans))
        if any(it["input"] and it["input"] in a for it, a in zip(items, ans)):
            # official quirk, bundle-wide: one echoing answer skips the sample
            results.append([False])
            skipped.append(True)
            metas.append({"skipped": "input echoed in answer"})
            subs.append([False] * len(items))
            continue
        sub = []
        for it, a in zip(items, ans):
            ok, meta = _eval_exec_sample(it["input"], it["code"], it["output"],
                                         a, timeout)
            sub.append(bool(ok))
        results.append([all(sub)])
        skipped.append(False)
        metas.append({"sub": sub})
        subs.append(sub)
    return {"qid": qid, "kind": "execb", "results": results,
            "skipped": skipped, "meta": metas, "sub": subs}


def eval_top_bundle_problem(qid, payload_str, preds):
    items = json.loads(payload_str)["items"]
    results, metas, subs = [], [], []
    for pred in preds:
        pr = list(pred) if isinstance(pred, (list, tuple)) else [pred]
        pr = pr + [""] * (len(items) - len(pr))
        sub = [bool(_check_top(a, it["expected"])) for a, it in zip(pr, items)]
        results.append([all(sub)])
        metas.append({"sub": sub})
        subs.append(sub)
    return {"qid": qid, "kind": "topb", "results": results, "meta": metas,
            "sub": subs}


def eval_one_problem(job):
    """job = (qid, payload, answers, timeout[, scenario]). scenario picks the
    grader: codegen (default), exec, top, execb, topb, math, mcq."""
    qid = job[0]
    in_out_str, codes, timeout = job[1], job[2], job[3]
    scenario = job[4] if len(job) > 4 else "codegen"
    if scenario == "exec":
        return eval_exec_problem(qid, in_out_str, codes, timeout)
    if scenario == "top":
        return eval_top_problem(qid, in_out_str, codes)
    if scenario == "execb":
        return eval_exec_bundle_problem(qid, in_out_str, codes, timeout)
    if scenario == "topb":
        return eval_top_bundle_problem(qid, in_out_str, codes)
    if scenario in ("math", "mcq"):
        from .grading_qa import eval_math_problem, eval_mcq_problem
        fn = eval_math_problem if scenario == "math" else eval_mcq_problem
        return fn(qid, in_out_str, codes)
    in_outs = json.loads(in_out_str)
    results, metas = [], []
    for code in codes:
        res, meta = _eval_one_generation(code, in_outs, timeout)
        results.append(res)
        metas.append(meta)
    return {"qid": qid, "results": results, "meta": metas}
