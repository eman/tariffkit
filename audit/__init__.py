"""Auditing computed bills against the statements PG&E actually issued.

Every defect this library has shipped was found the same way: by checking a
computed bill against a real statement, by hand, once. A CCA account priced as
bundled, a baseline credit frozen at the cycle's first day, vintage tables
inherited from the future, invented CCA credit, a state tax modelled nowhere --
each looked entirely plausible until a piece of paper disagreed. Nothing re-ran
those checks afterwards, so a regression in any of them would ship in silence
and the next statement would have to be reconciled by hand to find it.

This lives outside ``src/tariffkit`` deliberately. The public package owns the
provider statement importer, account profiles, and pricing; this harness owns
the PG&E-specific reconciliation map, attribution, orchestration, and portal
protocol notes.

The line is drawn at authentication rather than at "is it network code":
``tariffkit.sources.pge`` owns the session and the Green Button download,
because a metered record fetched over HTTP is still a metered record and that is
what ``sources`` is for. The printed-label map and reconciler remain here,
because they are facts about this audit's bill layout.

Both halves need the same login, so the session object is public and reusable
and this package never handles credentials itself.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
