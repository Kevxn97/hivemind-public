#!/usr/bin/env python3
"""Offer bounded local project context to a Codex SessionStart hook."""
import argparse
import json
from pathlib import Path
import sys
if __package__ and __package__.startswith('hivemind'):
    from ..runtime_paths import default_vault
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from runtime_paths import default_vault
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=default_vault())
    args = parser.parse_args()
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return
        hive = Path(__file__).resolve().parents[1] / "hive.py"
        result = subprocess.run(
            [sys.executable, str(hive), '--root', str(args.root), "brief", '--hook', '--agent', 'codex', "--budget", "500"],
            input=json.dumps({key: event[key] for key in ('cwd', 'session_id', 'source', 'hook_event_name', 'reason') if key in event}),
            capture_output=True, text=True, timeout=8, check=True,
        )
        if result.stdout.strip():
            print(json.dumps(json.loads(result.stdout), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Missing context and unavailable HiveMind must not block Codex.
        return


if __name__ == "__main__":
    main()
