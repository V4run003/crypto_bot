import logging
import sys


def setup(level=logging.INFO):
    """Configure root logging once at bot startup."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
