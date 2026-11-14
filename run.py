#!/usr/bin/env python3
"""Run HiveMind from the downloaded source directory."""
import sys

sys.dont_write_bytecode = True

if sys.version_info < (3, 11):
    print('HiveMind requires Python 3.11 or newer. Install it from https://www.python.org/downloads/ '
          'and run this command with that interpreter.', file=sys.stderr)
    raise SystemExit(1)

from hivemind.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
