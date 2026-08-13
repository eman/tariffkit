"""Reading what a statement says."""

from __future__ import annotations

from .model import Section, Statement, StatementLine, StatementSection
from .parse import parse_statement, read_statement

__all__ = [
    "Section",
    "Statement",
    "StatementLine",
    "StatementSection",
    "parse_statement",
    "read_statement",
]
