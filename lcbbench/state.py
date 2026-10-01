"""Shared settings that several parts of the bench read or write.
Always used as `state.NAME` so every module sees the same value."""

# Set by sampling.filter_sample so a run's summary can say what its sample was.
SAMPLE_NOTE = ""
# question_ids of the hardest tier of the current sample.
HARDEST_IDS = set()
# What the pool the sample came from looks like (rows, questions, labels, dates).
POOL_FACTS = {}
# The --mix spec as typed.
MIX_SPEC = ""
# True while the bench console may read the keyboard between two problems.
CONSOLE_LIVE = False
# The run folder being written right now (the console refuses to delete it).
CONSOLE_ACTIVE = {"run": None}
# Bearer token for servers that want one (--api-key or LCB_API_KEY).
API_KEY = None
