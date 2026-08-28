"""Command line surface.

    autopilot dry-run   --stories FILE [--date D]   build everything, contact a
                                                    mock platform, send nothing
    autopilot publish   --stories FILE [--date D]   the audited path
    autopilot recover   --stories FILE [--date D]   same path, after the cutoff,
                                                    at most once per issue
    autopilot status    [--date D]
    autopilot cancel    --date D --confirm

`publish` against a real platform requires an adapter to be registered. Out of
the box only the mock adapter exists, so an unconfigured checkout can do
everything except email a human being, which is the correct default for a repo
anyone can clone.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .adapters.mock import MockPublisher
from .config import Config
from .notify import ConsoleNotifier
from .pipeline import Pipeline

ADAPTERS = {"mock": MockPublisher}


def _today(cfg: Config) -> str:
    return dt.datetime.now(cfg.policy.tz).date().isoformat()


def _build(args: argparse.Namespace) -> tuple[Pipeline, Config]:
    cfg = Config.load(args.config)
    if args.home:
        cfg = cfg.with_home(args.home)
    if args.adapter not in ADAPTERS:
        raise SystemExit(f"unknown adapter {args.adapter!r}. Available: "
                         f"{', '.join(sorted(ADAPTERS))}")
    adapter = ADAPTERS[args.adapter](cfg.home / "platform",
                                     publication=cfg.publication_name,
                                     timezone=cfg.policy.timezone)
    return Pipeline(adapter, cfg, ConsoleNotifier()), cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autopilot", description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=None, help="TOML policy file")
    ap.add_argument("--home", default=None, help="where state, ledgers and output live")
    ap.add_argument("--adapter", default="mock", choices=sorted(ADAPTERS))
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("dry-run", "publish", "recover"):
        p = sub.add_parser(name)
        p.add_argument("--stories", required=True, type=Path)
        p.add_argument("--date", default=None)
        p.add_argument("--title", default=None)

    s = sub.add_parser("status")
    s.add_argument("--date", default=None)

    c = sub.add_parser("cancel")
    c.add_argument("--date", required=True, help="required: there is no 'cancel next'")
    c.add_argument("--confirm", action="store_true")

    args = ap.parse_args(argv)
    pipe, cfg = _build(args)
    date = getattr(args, "date", None) or _today(cfg)

    if args.cmd == "status":
        print(json.dumps(pipe.status(date), indent=2))
        return 0

    if args.cmd == "cancel":
        if not args.confirm:
            print("refusing to cancel without --confirm")
            return 1
        res = pipe.cancel(date)
        print(f"{res.status}: {res.detail}")
        return res.exit_code

    res = pipe.publish(date, args.stories, dry_run=(args.cmd == "dry-run"),
                       recovery=(args.cmd == "recover"), title=args.title)
    print(f"\n{res.status.upper()}: {res.detail}")
    print(f"stages completed: {' -> '.join(res.stages) or '(none)'}")
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
