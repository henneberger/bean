"""Plugin eval harness — drive bean's skill with a real tool-calling LLM (DeepSeek) instead of
Claude, over a seeded offline fixture, and assert the model composes the right retrieval commands
and cites its sources.

This tests the *plugin* layer (skills/bean/SKILL.md driving scripts/bean.py), which the offline
unit tests can't reach. DeepSeek's API is OpenAI-compatible, so it stands in for Claude as the
agent that reads the skill and calls tools — the portable signal being "these instructions
successfully drive a competent tool-caller."

Run:  .venv/bin/python dev/eval/run_eval.py         # local (reads $DEEPSEEK_API_KEY or $DEEPSEEK_API)
Skips cleanly (exit 0) when no key is present, so forks / the offline gate stay green.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))       # import bean
sys.path.insert(0, str(HERE))       # import eval_embed / fixture / scenarios

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
MAX_STEPS = 6
OUTPUT_CAP = 6000  # chars of bean stdout handed back to the model per call


def api_key() -> str | None:
    return os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API")


# --- the tool the model drives -----------------------------------------------------------------

def run_bean(args_str: str, env: dict) -> str:
    """Execute `bean <args>` against the fixture workspace and return stdout (capped)."""
    try:
        argv = shlex.split(args_str)
    except ValueError as e:
        return f"[harness] could not parse arguments: {e}"
    proc = subprocess.run([sys.executable, "-m", "bean", *argv],
                          capture_output=True, text=True, env=env, cwd=env["_CWD"])
    out = proc.stdout.strip()
    if proc.returncode != 0:
        out = (out + "\n" + proc.stderr.strip()).strip() or f"[exit {proc.returncode}]"
    return out[:OUTPUT_CAP]


TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_bean",
        "description": "Run a bean subcommand against the already-synced local index and return its "
                       "text output. Pass only the subcommand and its args, e.g. "
                       "'search \"retry backoff\" --source github' or 'recent --source slack'.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string",
                                       "description": "the bean subcommand line, without the leading 'bean'"}},
            "required": ["command"],
        },
    },
}]


def system_prompt() -> str:
    skill = (REPO / "skills" / "bean" / "SKILL.md").read_text()
    return (
        "You are being evaluated in an automated harness that stands in for Claude Code. "
        "Instead of running any shell command yourself, call the run_bean(command) function with "
        "just a bean subcommand line (no leading 'bean', no 'python3 .../bean.py'). The local index "
        "is already connected and synced — do NOT run init or sync. Compose retrieval commands as "
        "needed, then give a concise final answer that cites each source by its title. If nothing "
        "relevant is found, say so plainly instead of inventing an answer.\n\n"
        "The skill's retrieval toolbox is documented below.\n\n" + skill
    )


# --- one agent run -----------------------------------------------------------------------------

def _post(payload: dict, key: str) -> dict:
    body = json.dumps(payload).encode()
    req = urlrequest.Request(API_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    last = None
    for attempt in range(4):
        try:
            with urlrequest.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except (HTTPError, URLError, TimeoutError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"DeepSeek API failed after retries: {last}")


def agent_run(question: str, key: str, env: dict) -> dict:
    messages = [{"role": "system", "content": system_prompt()},
                {"role": "user", "content": question}]
    commands = []
    for _ in range(MAX_STEPS):
        data = _post({"model": MODEL, "messages": messages, "tools": TOOLS,
                      "temperature": 0.0}, key)
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"commands": commands, "answer": msg.get("content") or ""}
        messages.append(msg)
        for call in calls:
            try:
                cmd = json.loads(call["function"]["arguments"]).get("command", "")
            except json.JSONDecodeError:
                cmd = ""
            output = run_bean(cmd, env) if cmd else "[harness] empty command"
            commands.append({"args": cmd, "output": output})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    return {"commands": commands, "answer": "[harness] hit step limit without a final answer"}


# --- driver ------------------------------------------------------------------------------------

def main() -> int:
    key = api_key()
    if not key:
        print("plugin-eval: no DEEPSEEK_API_KEY / DEEPSEEK_API set — skipping (offline gate stays green).")
        return 0

    import fixture  # noqa: E402  (needs sys.path set above)
    from scenarios import SCENARIOS
    from bean.workspace import set_bean_home

    home = Path(tempfile.mkdtemp(prefix="bean-eval-home-"))
    work = Path(tempfile.mkdtemp(prefix="bean-eval-repo-"))
    set_bean_home(home / ".bean")           # this process (fixture builder)
    os.chdir(work)
    print(f"plugin-eval: seeding fixture under {home}/.bean …")
    fixture.build()

    # The CLI subprocesses share bean_home via HOME (bean_home = ~/.bean) and cwd via _CWD.
    env = {**os.environ, "HOME": str(home), "_CWD": str(work)}

    def one(item):
        key_name, question, check = item
        try:
            transcript = agent_run(question, key, env)
            ok, detail = check(transcript)
        except Exception as e:  # a scenario blowing up is a failure, not a crash
            return key_name, False, f"harness error: {e}", {"commands": [], "answer": ""}
        return key_name, ok, detail, transcript

    print(f"plugin-eval: running {len(SCENARIOS)} scenarios against {MODEL} …\n")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, SCENARIOS))

    failed = 0
    for name, ok, detail, transcript in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if not ok:
            failed += 1
            chose = " ; ".join(c["args"] for c in transcript["commands"]) or "(no commands)"
            print(f"      {detail}")
            print(f"      chose: {chose}")
            print(f"      answer: {transcript['answer'][:280]}")

    total = len(results)
    print(f"\nplugin-eval: {total - failed}/{total} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
