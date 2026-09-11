"""``python -m tariffkit.cli``, the same entry point as the installed script."""

from __future__ import annotations

import sys

from .commands import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
