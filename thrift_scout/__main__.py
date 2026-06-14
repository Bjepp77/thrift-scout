"""CLI entry point: ``python -m thrift_scout run``."""
from __future__ import annotations

import argparse
import logging
import sys


def main() -> None:
    p = argparse.ArgumentParser(description="Thrift Scout — ShopGoodwill monitor")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")
    rp = sub.add_parser("run", help="Execute a scan cycle")
    rp.add_argument("--config", default="config.yaml")
    rp.add_argument("--preview", metavar="FILE", help="Write HTML to file instead of emailing")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "run":
        from thrift_scout.run import run
        run(config_path=args.config, preview_html=args.preview)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
