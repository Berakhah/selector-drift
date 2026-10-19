"""python -m selector_drift <command>. No Typer, no Click, no [project.scripts].

The buyer's entrypoint is the Prefect scheduler
(`python -m selector_drift.orchestrate.deployments`); this is the developer's. Task 34 adds
`create-operator`.
"""

from __future__ import annotations

import argparse
import getpass
import sys

MIN_PASSWORD = 12


def _create_operator(username: str) -> int:
    from selector_drift.api.auth import USERNAME_RE

    if not USERNAME_RE.fullmatch(username):
        print("username must match ^[a-z0-9_.-]{1,64}$", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print(
            "create-operator reads the password from a terminal only - never an argument, "
            "never an environment variable. Refusing.",
            file=sys.stderr,
        )
        return 2
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Again: "):
        print("passwords differ", file=sys.stderr)
        return 2
    if len(password) < MIN_PASSWORD:
        print(f"use at least {MIN_PASSWORD} characters", file=sys.stderr)
        return 2
    # Imported here: create-operator must not pull in the orchestrator and its Playwright
    # dependency tree just to hash a password.
    from sqlalchemy.exc import IntegrityError

    from selector_drift.api.auth import create_operator
    from selector_drift.db.session import Session

    try:
        with Session() as s:
            create_operator(s, username, password)
            s.commit()
    except IntegrityError:
        print(f"operator {username} already exists", file=sys.stderr)
        return 2
    print(f"operator {username} created")
    return 0


def _run(spec: str) -> int:
    # Imported here, not at module top: `python -m selector_drift --help` needs no Playwright.
    from selector_drift.orchestrate.flows import extraction_flow

    print(extraction_flow(spec))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m selector_drift")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one extraction now, outside the schedule")
    run.add_argument(
        "spec", help="a spec file name such as testbench.yaml, resolved under SD_SPEC_DIR"
    )
    op = sub.add_parser("create-operator", help="create an API operator (password from a TTY)")
    op.add_argument("username")
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(str(args.spec))
    if args.command == "create-operator":
        return _create_operator(str(args.username))
    parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
