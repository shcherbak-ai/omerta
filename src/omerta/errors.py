"""The error that ends a run."""

from __future__ import annotations


class OmertaError(Exception):
    """A failure that ends a run with exit code 2.

    Its message says what failed and where (a file, a line, a git command),
    never a value of an env file.
    """
