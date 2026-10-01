"""Pull the answer out of what the model wrote (code, assertion, letter...)."""

import re


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


def _norm_nospace(s):
    return "".join(str(s).split())


def _exec_rhs(line):
    """Right-hand side of one answer assertion line, like extract_exec_answer
    does for a single-call answer."""
    if "==" not in line:
        return ""
    return extract_exec_answer(line, cot=False)


def extract_exec_bundle_answers(model_output, inputs, cot=False):
    """One answer string -> K per-call answers (missing ones as "").

    Matching is by the call text first (the line holding `assert f(5)` is the
    answer for the `f(5)` input), then by position among the leftovers, so a
    model that answers in order but without repeating the call text still
    gets counted. The grader stays strict: an answer that is not exactly the
    evaluated output fails."""
    text = model_output or ""
    block = ""
    if "[ANSWER]" in text:
        block = text.rsplit("[ANSWER]", 1)[-1]
        if "[/ANSWER]" in block:
            block = block.split("[/ANSWER]")[0]
    lines_src = block if any("==" in ln for ln in block.splitlines()) else text
    lines = [ln.strip() for ln in lines_src.splitlines() if ln.strip()]
    if not any("==" in ln for ln in lines):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    def unanswered(ln):
        rhs = ln.split("==")[-1].strip()
        return (not rhs or not rhs.rstrip(".?") or
                (rhs.startswith("?") and set(rhs) <= set("?. ")))

    used, out = set(), []
    for inp in inputs:
        key = _norm_nospace(inp)
        pick = None
        if key:
            for j, ln in enumerate(lines):
                if j in used or "==" not in ln or unanswered(ln):
                    continue
                if key in _norm_nospace(ln):
                    pick = j
                    break
        if pick is None:
            for j, ln in enumerate(lines):
                if j in used or "==" not in ln or unanswered(ln):
                    continue
                pick = j
                break
        if pick is not None:
            used.add(pick)
        out.append(_exec_rhs(lines[pick]) if pick is not None else "")
    return out


def extract_top_bundle_answers(model_output, call_strs):
    """One answer string -> K per-test assert lines (missing ones as "").
    Line i answers call i: matched by the call text when present, else taken
    in order among the leftover assert lines."""
    text = model_output or ""
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip().startswith("assert")]
    if len(lines) < len(call_strs):
        fenced = extract_top_answer(text)
        if fenced:
            more = [ln.strip() for ln in fenced.splitlines() if ln.strip()]
            if len(more) > len(lines):
                lines = more
    used, out = set(), []
    for cs in call_strs:
        key = _norm_nospace(cs)
        pick = None
        if key:
            for j, ln in enumerate(lines):
                if j in used:
                    continue
                if key in _norm_nospace(ln):
                    pick = j
                    break
        if pick is None:
            for j, ln in enumerate(lines):
                if j not in used:
                    pick = j
                    break
        if pick is not None:
            used.add(pick)
            out.append(lines[pick])
        else:
            out.append("")
    return out


# ---- math and multiple choice answers (fast suite)

def last_boxed(text):
    """Content of the last \\boxed{...} in text (balanced braces), or None."""
    if not text:
        return None
    idx = text.rfind("\\boxed")
    while idx != -1:
        start = text.find("{", idx)
        if start != -1 and text[idx + 6:start].strip() == "":
            depth = 0
            for j in range(start, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start + 1:j]
        idx = text.rfind("\\boxed", 0, idx)
    return None


_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*(?:/\d+)?")


def extract_math_answer(model_output):
    """The final answer of a math solution: last boxed, else an 'answer is'
    line, else the last number in the text. Empty string when nothing."""
    text = model_output or ""
    boxed = last_boxed(text)
    if boxed is not None:
        return boxed.strip()
    hits = re.findall(r"(?:final answer|answer)\s*(?:is|:)\s*([^\n]+)", text,
                      flags=re.IGNORECASE)
    if hits:
        return hits[-1].strip().strip("*$. ")
    nums = _NUM_RE.findall(text)
    return nums[-1] if nums else ""


def extract_mcq_letter(model_output, n_options=10):
    """The option letter the model chose, or "" when none can be read.
    Order: boxed letter, last 'Answer: X', 'answer is X', then nothing (a loose
    guess from stray capital letters would hand out free points)."""
    text = model_output or ""
    valid = "ABCDEFGHIJKLMNOP"[:max(2, n_options)]
    boxed = last_boxed(text)
    if boxed is not None:
        m = re.search(r"\b([A-P])\b", boxed.replace("\\text", ""))
        if m and m.group(1) in valid:
            return m.group(1)
    pats = [r"(?i:answer)\s*:\s*\**\s*\(?\s*([A-P])\s*\)?(?![A-Za-z])",
            r"(?i:answer)\s+(?i:is)\s*:?\s*\**\s*\(?\s*([A-P])\s*\)?(?![A-Za-z])"]
    for pat in pats:
        hits = [h for h in re.findall(pat, text) if h in valid]
        if hits:
            return hits[-1]
    return ""
