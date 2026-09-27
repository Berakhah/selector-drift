"""The mint control for `AuthorisedTarget`, and nothing else.

A leading underscore is a convention, not a control (M29): `from selector_drift.scope._token
import _ScopeToken` works from anywhere Python can see this module. The real enforcement is
two-fold: `AuthorisedTarget.__post_init__` checks *identity* against `_MINT`, the one instance
this module ever constructs, not merely `isinstance`; and the `only-the-gate-mints` import-linter
contract in `.importlinter` forbids every other package from importing this module directly.
Kept in its own file so that contract has something to point at.
"""

from __future__ import annotations


class _ScopeToken:
    __slots__ = ()


_MINT = _ScopeToken()  # the only instance the gate will accept
