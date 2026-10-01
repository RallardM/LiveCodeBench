"""Graders for math (final answer) and multiple choice (option letter).
No subprocess needed: both are pure text comparisons, so they are instant."""

import json
import re
from fractions import Fraction

from .extract import extract_math_answer, extract_mcq_letter


def _strip_math(s):
    s = str(s).strip()
    s = s.replace("$", "").replace("\\!", "").replace("\\,", "").replace("\\;", "")
    s = s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = s.replace("\\left", "").replace("\\right", "")
    s = re.sub(r"\\text\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", s)
    s = s.replace("^\\circ", "").replace("^{\\circ}", "").replace("\\%", "").replace("%", "")
    s = s.replace("\\ ", "").replace(" ", "")
    s = re.sub(r"\\sqrt(\d)", r"\\sqrt{\1}", s)
    s = s.rstrip(".")
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    return s


def _to_number(s):
    """A Fraction for integers, decimals, a/b and \\frac{a}{b}; else None."""
    s = s.strip()
    m = re.fullmatch(r"(-?)\\frac\{(-?\d+)\}\{(-?\d+)\}", s)
    if m:
        try:
            val = Fraction(int(m.group(2)), int(m.group(3)))
        except ZeroDivisionError:
            return None
        return -val if m.group(1) else val
    m = re.fullmatch(r"(-?\d+)/(-?\d+)", s)
    if m:
        try:
            return Fraction(int(m.group(1)), int(m.group(2)))
        except ZeroDivisionError:
            return None
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return Fraction(s)
    return None


def math_equal(pred, gold):
    """Same final answer? Exact after light latex clean-up, or equal as
    numbers. Strict on purpose: an exotic but equal form can miss (a false
    negative costs the same for every model, a false positive never happens)."""
    p, g = _strip_math(pred), _strip_math(gold)
    if not p or not g:
        return False
    if p == g:
        return True
    pn, gn = _to_number(p), _to_number(g)
    if pn is not None and gn is not None:
        return pn == gn
    try:  # optional, only when the package happens to be installed
        from math_verify import parse, verify
        return bool(verify(parse("$" + g + "$"), parse("$" + p + "$")))
    except Exception:
        return False


def eval_math_problem(qid, payload_str, answers):
    gold = json.loads(payload_str)["gold"]
    results = []
    for out in answers:
        pred = extract_math_answer(out if isinstance(out, str) else "")
        results.append([bool(math_equal(pred, gold))])
    return {"qid": qid, "kind": "math", "results": results,
            "meta": [{"gold": gold}] * len(answers)}


def eval_mcq_problem(qid, payload_str, answers):
    d = json.loads(payload_str)
    gold, n_opts = d["gold"], d.get("n_options", 10)
    results = []
    for out in answers:
        letter = extract_mcq_letter(out if isinstance(out, str) else "", n_opts)
        results.append([bool(letter) and letter == gold])
    return {"qid": qid, "kind": "mcq", "results": results,
            "meta": [{"gold": gold}] * len(answers)}
