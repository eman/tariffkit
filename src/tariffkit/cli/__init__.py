"""The command line interface, and the account storage only it owns.

Reading and writing the account file is an application concern, not a library
one: it decides where a machine keeps its files, how they are locked, and who
is allowed to read them. The library is handed the account it should price
with. Keeping the two apart is what stops ``tariffkit.web`` and
``tariffkit.mqtt`` from quietly reading ``~/.config`` on import, so an embedder
that already has an :class:`~tariffkit.account.AccountProfile` -- Home
Assistant holds one in its config entry -- never touches this file at all.
"""

from __future__ import annotations

from .account_store import AccountStore
from .commands import build_parser, main

__all__ = ["AccountStore", "build_parser", "main"]
