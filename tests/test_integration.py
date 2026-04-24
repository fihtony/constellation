#!/usr/bin/env python3
"""Container-oriented integration wrapper that delegates to dedicated agent tests."""

from __future__ import annotations

import argparse

from test_scm_agent import main as run_scm
from test_tracker_agent import main as run_tracker


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracker", action="store_true")
    parser.add_argument("--scm", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    run_all = not any((args.tracker, args.scm))
    selected = []
    if args.tracker or run_all:
        selected.append(run_tracker)
    if args.scm or run_all:
        selected.append(run_scm)

    exit_code = 0
    child_args = ["--container"]
    if args.verbose:
        child_args.append("--verbose")

    for runner in selected:
        result = runner(child_args)
        exit_code = exit_code or result
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())