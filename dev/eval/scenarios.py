"""Eval scenarios: a question + invariant checks over the agent's transcript.

Each check asserts on *invariants* (which tool the model chose, whether the answer cites the
fixture doc that holds the answer), never exact prose — LLM output varies run to run. A transcript
is {"commands": [{"args": str, "output": str}, ...], "answer": str}.
"""

from __future__ import annotations


def _cmds(t) -> str:
    return " ||| ".join(c["args"].lower() for c in t["commands"])


def _ran(t, *needles) -> bool:
    blob = _cmds(t)
    return any(n in blob for n in needles)


def _answer_has(t, *needles) -> bool:
    a = (t["answer"] or "").lower()
    return any(n.lower() in a for n in needles)


def _says_nothing(t) -> bool:
    return _answer_has(t, "no results", "nothing", "couldn't find", "could not find",
                       "not in", "no match", "didn't find", "did not find", "no relevant")


# (key, question, check) — check(transcript) -> (ok: bool, detail: str)
SCENARIOS = [
    (
        "recency",
        "What's the latest in the incidents channel?",
        lambda t: (
            _ran(t, "recent", "search") and _answer_has(t, "5xx", "checkout"),
            "expected a recent/search call and an answer citing the checkout 5xx incident",
        ),
    ),
    (
        "codebase-impact",
        "The incidents thread blames retries with no backoff — where in the code is that?",
        lambda t: (
            _ran(t, "search") and _answer_has(t, "retry.py", "retry_loop", "timeouts.yaml"),
            "expected a search call and an answer citing the retry code",
        ),
    ),
    (
        "identifier",
        "find ZQ-9001",
        lambda t: (
            _ran(t, "zq-9001") and _answer_has(t, "incident", "checkout", "5xx", "payments"),
            "expected the identifier passed to search and an answer tying it to the incident",
        ),
    ),
    (
        "whole-doc",
        "Show me what the onboarding guide says.",
        lambda t: (
            _ran(t, "onboarding", "doc", "search") and _answer_has(t, "google", "slack"),
            "expected a doc/search call and an answer naming the Google + Slack setup steps",
        ),
    ),
    (
        "graceful-empty",
        "What does our HIPAA compliance policy say?",
        lambda t: (
            _ran(t, "search", "recent") and _says_nothing(t)
            and not _answer_has(t, "hipaa requires", "the policy states", "according to the policy"),
            "expected the model to query, then say nothing was found instead of fabricating a policy",
        ),
    ),
]
