"""--self-test: sanity-check the grader and the answer extractors. No model
server and no internet needed."""

import json
import time

from .bundling import _bundle_chunks
from .extract import (extract_exec_answer, extract_exec_bundle_answers,
                      extract_math_answer, extract_mcq_letter, extract_top_answer,
                      extract_top_bundle_answers)
from .grading import eval_one_problem
from .grading_qa import math_equal
from .suite_data import SKILLS, TIERS, item_sequence


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

    # --- --bundle checks: K calls in one answer, all-or-nothing -------------
    xb_payload = json.dumps({"items": [
        {"code": "def f(x):\n    return x * 2\n", "input": "f(3)", "output": "6"},
        {"code": "def h(x):\n    return x + 1\n", "input": "h(4)", "output": "5"},
    ]})
    xb_answers = [
        ["6", "5"],        # all right -> item right
        ["6", "4"],        # one wrong -> item wrong (that is the whole point)
        ["6"],             # missing answer -> wrong
        ["f(3)", "5"],     # echoes an input -> skipped, official quirk
    ]
    rec = eval_one_problem(("selftest", xb_payload, xb_answers, timeout, "execb"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, False, False]
    bok = flags == want and rec.get("skipped") == [False, False, False, True]
    subs_ok = (rec["sub"][0] == [True, True] and rec["sub"][1] == [True, False])
    ok = ok and bok and subs_ok
    print(f"[{'OK ' if bok and subs_ok else 'FAIL'}] exec bundle (all-or-nothing"
          f" + per-call sub): got {flags} skipped={rec.get('skipped')} want {want}"
          f" sub0={rec['sub'][0]} sub1={rec['sub'][1]}")

    tb_payload = json.dumps({"items": [{"expected": "6"}, {"expected": "7"}]})
    tb_preds = [
        ["assert f(3) == 6", "assert g(2) == 7"],
        ["assert f(3) == 6", "assert g(2) == 8"],
        ["assert f(3) == 6"],
    ]
    rec = eval_one_problem(("selftest", tb_payload, tb_preds, timeout, "topb"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, False]
    tb_ok = flags == want
    ok = ok and tb_ok
    print(f"[{'OK ' if tb_ok else 'FAIL'}] top bundle (all-or-nothing):"
          f" got {flags} want {want}")

    xb_extract = [
        ("execb in-order block",
         extract_exec_bundle_answers(
             "[ANSWER]\nassert f(3) == 6\nassert h(4) == 5\n[/ANSWER]",
             ["f(3)", "h(4)"]), ["6", "5"]),
        ("execb matched by call text",
         extract_exec_bundle_answers(
             "[ANSWER]\nassert h(4) == 5\nassert f(3) == 6\n[/ANSWER]",
             ["f(3)", "h(4)"]), ["6", "5"]),
        ("execb cot block",
         extract_exec_bundle_answers(
             "step by step...\n[ANSWER]\nassert f(3) == 6\nassert h(4) == 5\n"
             "[/ANSWER]", ["f(3)", "h(4)"], cot=True), ["6", "5"]),
        ("execb missing stays empty",
         extract_exec_bundle_answers("[ANSWER]\nassert f(3) == 6\n[/ANSWER]",
                                     ["f(3)", "h(4)"]), ["6", ""]),
        ("topb assert lines",
         extract_top_bundle_answers(
             "here:\n```python\nassert f(3) == 6\nassert g(2) == 7\n```",
             ["f(3)", "g(2)"]),
         ["assert f(3) == 6", "assert g(2) == 7"]),
    ]
    for name, got, want in xb_extract:
        eok = got == want
        ok = ok and eok
        print(f"[{'OK ' if eok else 'FAIL'}] {name}: got {got!r} want {want!r}")

    fake_rows = ([{"question_id": f"7#{i}", "difficulty": "medium"}
                  for i in range(5)]
                 + [{"question_id": f"8#{i}", "difficulty": "easy"}
                    for i in range(3)])
    chunks = _bundle_chunks(fake_rows, 2)
    bok2 = ([c[2][0]["question_id"] for c in chunks] == ["7#0", "7#2", "8#0"]
            and all(len(c[2]) == 2 for c in chunks))
    ok = ok and bok2
    print(f"[{'OK ' if bok2 else 'FAIL'}] bundle chunking is deterministic and"
          f" drops the leftover of a question (5+3 rows -> 3 pairs)")

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

    # --- suite checks: math and multiple choice graders ---------------------
    math_payload = json.dumps({"gold": "\\frac{1}{2}"})
    math_answers = ["so \\boxed{0.5}", "the answer is \\boxed{\\dfrac12}",
                    "\\boxed{2}", "no idea"]
    rec = eval_one_problem(("selftest", math_payload, math_answers, timeout, "math"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, False, False]
    m_ok = flags == want
    ok = ok and m_ok
    print(f"[{'OK ' if m_ok else 'FAIL'}] math grader: got {flags} want {want}")

    mcq_payload = json.dumps({"gold": "C", "n_options": 4})
    mcq_answers = ["reasoning...\nAnswer: C", "Answer: B", "The answer is a valid one",
                   "\\boxed{C}"]
    rec = eval_one_problem(("selftest", mcq_payload, mcq_answers, timeout, "mcq"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, False, True]
    q_ok = flags == want
    ok = ok and q_ok
    print(f"[{'OK ' if q_ok else 'FAIL'}] mcq grader: got {flags} want {want}")

    qa_checks = [
        ("math boxed nested", extract_math_answer("x \\boxed{\\frac{1}{2}} y"), "\\frac{1}{2}"),
        ("math equal decimal", math_equal("0.75", "\\frac{3}{4}"), True),
        ("math equal thousands", math_equal("1,000", "1000"), True),
        ("math not equal", math_equal("5", "6"), False),
        ("mcq letter", extract_mcq_letter("Answer: (D)"), "D"),
    ]
    for name, got, want in qa_checks:
        eok = got == want
        ok = ok and eok
        print(f"[{'OK ' if eok else 'FAIL'}] {name}: got {got!r} want {want!r}")

    # the suite order: round r = item r of every cell, easy tier first
    fake = {s: {t: [f"{s}-{t}-{i}" for i in range(3)] for t in TIERS} for s in SKILLS}
    fake = {s: {t: [{"question_id": x} for x in v] for t, v in d.items()}
            for s, d in fake.items()}
    seq = [it["question_id"] for it in item_sequence(fake, 2)]
    s_ok = (len(seq) == 32 and seq[0] == "code-easy-0" and seq[15] == "reading-hardest-0"
            and seq[16] == "code-easy-1")
    ok = ok and s_ok
    print(f"[{'OK ' if s_ok else 'FAIL'}] suite order: {len(seq)} items, round 1 ends"
          f" at {seq[15]}, round 2 starts at {seq[16]}")

    print("\nSelf-test:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1
