"""The mint control for `AuthorisedTarget`, and nothing else.

A leading underscore is a convention, not a control (M29): `from selector_drift.scope._token
import _ScopeToken` works from anywhere Python can see this module. The real enforcement is
three-fold:

1. `AuthorisedTarget.__post_init__` checks *identity* against `_MINT`, the one instance this
   module ever constructs, not merely `isinstance`.
2. It ALSO checks that `_MINTING` -- a context-local flag only `gate.authorise()` sets, and only
   for the duration of the `AuthorisedTarget(...)` call -- is set. Identity on `_MINT` alone is
   not enough: `dataclasses.replace(some_authorised_target, origins=("*",))` passes the
   already-minted `_MINT` instance straight back through `__init__`, so a bare identity check
   would let `replace()` re-mint outside the gate. `_MINTING` closes that: `replace()` called
   anywhere other than inside `authorise()`'s own call finds the flag unset and is refused.
3. The `only-the-gate-mints` import-linter contract in `.importlinter` forbids every package
   listed there from importing this module directly.

Kept in its own file so (3) has something to point at. Nothing outside `selector_drift.scope`
may reference `_MINT` or `_MINTING` by name -- enforced by
`test_no_module_outside_scope_references_the_mint_internals` in `tests/scope/test_gate.py`.
"""

from __future__ import annotations

from contextvars import ContextVar


class _ScopeToken:
    __slots__ = ()


_MINT = _ScopeToken()  # the only instance the gate will accept

# Set to True only within gate.authorise()'s own construction of an AuthorisedTarget, reset in
# a `finally` immediately after. Never read or written by anything outside this package.
_MINTING: ContextVar[bool] = ContextVar("_MINTING", default=False)
