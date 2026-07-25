#!/usr/bin/env python3
"""Root entry point so the central builder's `make run` (which runs
`python main.py`) works. The real logic lives in the `wikimill` package."""
import sys
from pathlib import Path

# Make `wikimill` importable when run as a bare script (outside `uv run wikimill`).
sys.path.insert(0, str(Path(__file__).parent / "src"))

from wikimill.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
