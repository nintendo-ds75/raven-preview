"""Run one coding host against an already-onboarded HTTP Bridge workspace.

The operator completes onboarding and human replies in the real browser. This
driver supplies no decisions, owners, or answers. MCP config is a private local
file created by the Connections UI; never copy it into the results directory.
Requires Claude Code on PATH. With ANTHROPIC_API_KEY in the environment the host
runs in bare mode on that key; without it, on the CLI's own login, which reads
the machine's user settings and CLAUDE.md, so run it where those are empty.
"""

import argparse
import os
from pathlib import Path
import subprocess
import time


TASK = """Some upstream services send nonstandard Retry-After delay values such as
`0.5` and `+3`, and callers currently receive InvalidHeader. Improve interoperability
without unexpectedly changing existing retry behavior or creating retry storms.
Investigate the implementation, obtain needed policy decisions through Bridge,
implement a narrow change, and add and run regression tests.
"""

TEAM_INSTRUCTION = """This team uses Bridge (the `bridge` MCP server) to route the
judgment calls in a task to the person who owns them. At the start of every task,
before reading or editing anything, call `bridge_start_task`, then follow what
Bridge returns for the rest of the task.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--mcp-config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, help="A different task, for a sibling or resumed run")
    parser.add_argument("--client-key", default="urllib3-retry-live-user")
    parser.add_argument("--max-turns", type=int, default=45)
    parser.add_argument("--timeout", type=int, default=900, help="Seconds before the host is stopped")
    args = parser.parse_args()
    task = args.task_file.read_text() if args.task_file else TASK
    args.out.mkdir(parents=True, exist_ok=False)
    prompt = TEAM_INSTRUCTION + "\nTask:\n" + task + f"""
Repository identity: urllib3/urllib3. Use client_key={args.client_key}.
An actual test owner is using the Bridge web inbox; use bridge_wait when blocked.
Do not act as a human or bypass a refusal. Finish through Bridge with the actual
diff and the checks you actually ran. The prepared test interpreter is
{args.python}; run its pytest module against test/test_retry.py.
Stay in this checkout. Do not use subagents, install packages, access external
services other than Bridge, commit, or push. Do not inspect evaluation files or
credentials. Leave a reviewable patch and a concise final summary.
"""
    (args.out / "prompt.txt").write_text(prompt)
    # Bare mode reads only ANTHROPIC_API_KEY for auth; a machine signed in
    # to Claude Code instead has no key to give it.
    bare = ["--bare"] if os.environ.get("ANTHROPIC_API_KEY") else []
    command = ["claude", *bare, "-p", "--output-format", "stream-json",
               "--verbose", "--no-session-persistence", "--max-turns", str(args.max_turns),
               "--max-budget-usd", "4", "--permission-mode", "acceptEdits",
               "--strict-mcp-config", "--mcp-config", str(args.mcp_config),
               "--tools", "Read,Grep,Glob,Edit,Write,Bash", "--allowedTools",
               "mcp__bridge", "Bash(git diff *)", "Bash(git status *)",
               f"Bash({args.python} -m pytest *)"]
    started = time.monotonic()
    with (args.out / "host.jsonl").open("w") as out, (args.out / "host.stderr").open("w") as err:
        result = subprocess.run(command, cwd=args.repo, env=os.environ,
                                input=prompt, text=True, stdout=out, stderr=err,
                                timeout=args.timeout)
    (args.out / "exit.txt").write_text(
        f"exit={result.returncode}\nseconds={time.monotonic() - started:.1f}\n")
    print((args.out / "exit.txt").read_text())


if __name__ == "__main__":
    main()
