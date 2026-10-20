"""Only documentation changes skip application jobs; unknown baselines run everything."""

import os
from pathlib import Path
import subprocess


def needs_application(names):
    return any(not (
        (name.startswith("docs/") and name.endswith((".md", ".json")))
        or name in {"README.md", "AGENTS.md", "项目说明.md"}
    ) for name in names)


def select():
    if os.environ.get("EVENT") == "workflow_dispatch":
        return True
    base = os.environ.get("BASE") or os.environ.get("BEFORE")
    if not base or set(base) == {"0"}:
        return True
    result = subprocess.run(["git", "diff", "--no-renames", "--name-only", "-z", base, "HEAD"],
                            capture_output=True, check=False)
    if result.returncode:
        return True
    return needs_application(result.stdout.decode().strip("\0").split("\0"))


if __name__ == "__main__":
    value = str(select()).lower()
    print("application=" + value)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write("application=" + value + "\n")
