"""The ``flowers`` console command — a thin, stdlib-only launcher.

Deliberately tiny: argparse (no CLI framework — the core's no-dependency rule extends to its
entry point), and the web stack is imported lazily behind ``serve`` so ``flowers --version``
works without the ``[web]`` extra and a missing extra fails with the fix instruction, not a
traceback. Database paths are passed via the FLOWERS_DB / FLOWERS_TIMERS_DB environment
variables BEFORE the app module is imported — the app wires its stores at import time, and a
real environment variable always wins over ``.env`` (see ``flowers.runtime.load_dotenv``).
"""

from __future__ import annotations

import argparse
import os
import sys

import flowers


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("the web surface needs the [web] extra: pip install \"flowers[web]\"",
              file=sys.stderr)
        return 1
    if args.db:
        os.environ["FLOWERS_DB"] = args.db
    if args.timers_db:
        os.environ["FLOWERS_TIMERS_DB"] = args.timers_db
    # workers=1 is load-bearing, not a default: each worker process would run its own timer
    # poller and crash-recovery sweep against the same sqlite files.
    uvicorn.run("flowers.app:app", host=args.host, port=args.port, workers=1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flowers",
        description="a trustable agent with a deterministic, no-LLM verification gate")
    parser.add_argument("--version", action="version", version=f"flowers {flowers.__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="serve the dashboard + REST API (needs the [web] extra)")
    serve.add_argument("--host", default="127.0.0.1",
                       help="bind address (default 127.0.0.1 — there is no auth; only expose "
                            "beyond localhost on a network you trust)")
    serve.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    serve.add_argument("--db", default="", help="path to the run-state sqlite db (FLOWERS_DB)")
    serve.add_argument("--timers-db", default="",
                       help="path to the durable-timers sqlite db (FLOWERS_TIMERS_DB)")

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
