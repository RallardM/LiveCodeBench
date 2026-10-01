"""Every prompt the bench sends, one place. The codegen prompt is the official
LiveCodeBench chat prompt, verbatim (that is what the public leaderboard uses)."""

import ast

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


# ---- code_execution / test_output_prediction (official LiveCodeBench prompts)

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


# ---- --bundle K: K calls answered in ONE answer, all or nothing.

EXEC_BUNDLE_HEAD = (
    "You are given several short Python programs and, under each one, an "
    "assertion containing an input to that program. Complete EVERY assertion "
    "with a literal (no unsimplified expressions, no function calls) "
    "containing the output when executing that program on that input, even if "
    "a program is incorrect or incomplete. Do NOT output any extra "
    "information. Give one completed assertion per program, in the same order "
    "the programs appear, one per line, inside one pair of [ANSWER] and "
    "[/ANSWER] tags, following the examples."
)

EXEC_BUNDLE_HEAD_COT = (
    "You are given several short Python programs and, under each one, an "
    "assertion containing an input to that program. Complete EVERY assertion "
    "with a literal (no unsimplified expressions, no function calls) "
    "containing the output when executing that program on that input, even if "
    "a program is incorrect or incomplete. Do NOT output any extra "
    "information. Execute each program step by step before arriving at its "
    "answer, and give one completed assertion per program, in the same order "
    "the programs appear, one per line, inside one pair of [ANSWER] and "
    "[/ANSWER] tags, following the examples."
)

EXEC_BUNDLE_EXAMPLES = """[PYTHON]
def repeatNumber(number : int) -> int:
    return number
assert repeatNumber(number = 17) == ??
[/PYTHON]
[PYTHON]
def addCharacterA(string : str) -> str:
    return string + "a"
assert addCharacterA(string = "x9j") == ??
[/PYTHON]
[ANSWER]
assert repeatNumber(number = 17) == 17
assert addCharacterA(string = "x9j") == "x9ja"
[/ANSWER]"""


def format_prompt_exec_bundle(items, cot=False):
    """items: list of {"code", "input"} dicts, same order as the answers."""
    blocks = "".join(
        f"[PYTHON]\n{it['code']}\nassert {it['input']} == ??\n[/PYTHON]\n"
        for it in items)
    tmpl = (f"{EXEC_BUNDLE_HEAD_COT if cot else EXEC_BUNDLE_HEAD}\n\n"
            + EXEC_BUNDLE_EXAMPLES + "\n\n" + blocks
            + ("[THOUGHT]\n" if cot else "[ANSWER]\n"))
    return [{"role": "system", "content": EXEC_SYSTEM_MESSAGE},
            {"role": "user", "content": tmpl}]


def format_prompt_top_bundle(question_content, starter_code, function_name,
                             testcase_inputs):
    func_name = parse_function_name_from_starter_code(starter_code) or function_name
    prompt = f"Problem:\n{question_content}"
    prompt += f"Function:\n```\n{starter_code}\n```\n"
    prompt += ("Please complete ALL of the following test cases: one finished"
               " assert statement per line, in the order given, inside one"
               " python code block.\n\n```\n")
    for t in testcase_inputs:
        prompt += format_testcase_func_name_input(func_name, t) + "\n"
    prompt += "```\n"
    return [{"role": "system", "content": TOP_SYSTEM_MESSAGE},
            {"role": "user", "content": prompt}]


# ---- math and multiple choice (used by the fast suite)

QA_SYSTEM_MESSAGE = ("You are an expert problem solver. Think carefully, "
                     "then give one final answer in the exact format asked.")


def format_prompt_math(problem):
    text = ("Solve the following math problem. Reason step by step, then put "
            "the final answer inside \\boxed{} on the last line.\n\n"
            + problem.strip() + "\n")
    return [{"role": "system", "content": QA_SYSTEM_MESSAGE},
            {"role": "user", "content": text}]


def format_prompt_mcq(question, options):
    letters = "ABCDEFGHIJKLMNOP"
    lines = "\n".join(f"{letters[i]}. {opt}" for i, opt in enumerate(options))
    text = ("Answer the following multiple choice question. Reason step by "
            "step, then finish with a last line of exactly: Answer: X (X is "
            "the single letter of the correct option).\n\n"
            + question.strip() + "\n\n" + lines + "\n")
    return [{"role": "system", "content": QA_SYSTEM_MESSAGE},
            {"role": "user", "content": text}]
