"""python -m selector_drift <command>. No Typer, no Click, no [project.scripts].

The buyer's entrypoint is the Prefect scheduler
(`python -m selector_drift.orchestrate.deployments`); this is the developer's. Task 34 adds
`create-operator`.
"""

from __future__ import annotations

import argparse


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
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(str(args.spec))
    parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
