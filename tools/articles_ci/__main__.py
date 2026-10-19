"""`python3 -m tools.articles_ci` entry point."""

from __future__ import annotations

import sys

from .cli import main

raise SystemExit(main(sys.argv[1:]))
