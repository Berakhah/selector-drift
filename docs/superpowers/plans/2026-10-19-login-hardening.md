# Login Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three open unauthenticated-login problems from Task 34 (argon2 memory exhaustion, `audit_log` pollution, operator lockout) so `serve-api` and non-loopback deploys are unblocked.

**Architecture:** A process-local non-blocking semaphore caps concurrent argon2 verifies (refused with 429 when full). Failures for usernames that do not exist are counted in memory and logged without the username, never written to `audit_log`. An audited `unlock-operator` CLI command writes a `login_unlock` row, and the throttle counts only failures after the latest unlock.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, argon2-cffi, pytest (Postgres integration).

**Spec:** `docs/superpowers/specs/2026-09-26-selector-drift-api-contract.md` (auth section: 5 failures per 15 minutes, 429 `throttled`). Parked-item record: `.superpowers/sdd/2026-10-19-api-task-34/progress.md`.

## Global Constraints

- Failure semantics unchanged for existing usernames: 5 failures per 15 minutes, then 429 `throttled` with `Retry-After`.
- Responses for unknown vs wrong-password stay identical (401, same body, similar timing).
- No password, hash or username-of-an-unknown-user in any log line or audit row.
- `mypy --strict`, `ruff check`, `ruff format --check` clean; import contracts 11 kept, 0 broken.
- Test fixtures never contain committed secrets (use `secrets.token_urlsafe`, as `tests/api/conftest.py` does).
- Files: `selector_drift/api/auth.py`, `selector_drift/__main__.py`, `tests/api/test_hardening.py` (new tests go here; reuse `LOGIN`, `make_client`, fixtures from `tests/api/conftest.py`).

---

### Task 1: Global cap on concurrent argon2 verifies

**Files:**
- Modify: `selector_drift/api/auth.py` (around `authenticate`, lines ~85-125)
- Test: `tests/api/test_hardening.py`

**Interfaces:**
- Produces: `VERIFY_CAP: int = 4`; module-level `_verify_slots: threading.BoundedSemaphore`.

Each verify costs about 64 MiB, so a cap of 4 bounds auth memory near 256 MiB per process. Multiple uvicorn workers multiply this; the code comment must say so.

- [ ] **Step 1: Write the failing test**

```python
def test_a_full_verify_cap_is_refused_at_once_with_429(anon_client, operator, monkeypatch):
    import threading

    from selector_drift.api import auth

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(auth, "_verify_slots", slots)
    assert slots.acquire(blocking=False)  # every slot is busy
    try:
        r = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    finally:
        slots.release()
    assert r.status_code == 429 and r.json()["type"].endswith("throttled")
    assert "busy" in r.json()["detail"] and r.headers["Retry-After"] == "1"
    # a refused attempt is not a guess: it must not count toward the username's 5
    with Session() as s:
        n = s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "login_failure"))
    assert n == 0
    ok = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert ok.status_code == 200
```

Add imports `func`, `select`, `AuditLog`, `Session` at the top of the test file if missing.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/api/test_hardening.py::test_a_full_verify_cap_is_refused_at_once_with_429 -v`
Expected: FAIL with `AttributeError: ... has no attribute '_verify_slots'`.

- [ ] **Step 3: Implement**

In `auth.py` add `import threading`, then beside `_DUMMY_HASH`:

```python
# Each argon2id verify allocates ~64 MiB. Cap concurrent verifies per process so rotating
# usernames cannot exhaust memory; a full cap is refused at once, never queued. Per PROCESS:
# N uvicorn workers allow N * VERIFY_CAP.
VERIFY_CAP = 4
_verify_slots = threading.BoundedSemaphore(VERIFY_CAP)
```

In `authenticate`, replace the `ok: bool` / `try: ... except` block with:

```python
    if not _verify_slots.acquire(blocking=False):
        db.rollback()
        raise Throttled(
            "The server is busy verifying other sign-ins - try again shortly",
            headers={"Retry-After": "1"},
        )
    ok: bool
    try:
        ok = _hasher.verify(op.password_hash if usable and op else _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    finally:
        _verify_slots.release()
```

- [ ] **Step 4: Run the files to verify they pass**

Run: `uv run pytest tests/api/test_hardening.py tests/api/test_auth.py -v`
Expected: all PASS, including the existing burst and lock tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/api/auth.py tests/api/test_hardening.py
git commit -m "fix(api): cap concurrent argon2 verifies per process, refuse with 429 when full"
```

---

### Task 2: No audit rows for unknown usernames

**Files:**
- Modify: `selector_drift/api/auth.py` (failure branch of `authenticate`)
- Test: `tests/api/test_hardening.py`

**Interfaces:**
- Produces: `unknown_user_failures() -> int` (process-local aggregate counter, for later metrics).

A failure for a username that has no operator row is counted in memory and logged at WARNING **without the username**. Failures for existing operators (including disabled ones) are still audited, since the throttle needs them.

- [ ] **Step 1: Write the failing tests**

```python
def test_failures_for_unknown_usernames_write_no_audit_rows_but_are_counted(
    anon_client, operator, caplog
):
    from selector_drift.api.auth import unknown_user_failures

    before = unknown_user_failures()
    for i in range(8):
        r = anon_client.post(LOGIN, json={"username": f"ghost{i}", "password": "wrong-guess"})
        assert r.status_code == 401
    with Session() as s:
        n = s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "login_failure"))
    assert n == 0
    assert unknown_user_failures() == before + 8
    assert "ghost" not in caplog.text  # the typed name is attacker-controlled input


def test_a_wrong_password_for_a_real_operator_is_still_audited(anon_client, operator):
    anon_client.post(LOGIN, json={"username": "op1", "password": "wrong-guess"})
    assert latest_audit("login_failure").subject == "operator:op1"
```

Import `latest_audit` from `tests.api.conftest` if missing.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/api/test_hardening.py -k "unknown_usernames or still_audited" -v`
Expected: the first FAILs (`unknown_user_failures` is not defined), the second PASSes (it pins existing behaviour).

- [ ] **Step 3: Implement**

In `auth.py` add `import itertools`, `import logging`, then:

```python
_log = logging.getLogger(__name__)
_unknown_counter = itertools.count(1)  # next() is atomic under the GIL
_unknown_seen = 0


def unknown_user_failures() -> int:
    """Failed sign-ins for usernames with no operator row, since process start."""
    return _unknown_seen
```

Replace the failure branch with:

```python
    if not (ok and usable and op):
        if op is None:
            global _unknown_seen
            _unknown_seen = next(_unknown_counter)
            _log.warning("login failure for an unknown username (count=%d)", _unknown_seen)
            db.rollback()  # releases the xact lock; nothing to persist
        else:
            audit(db, actor="anonymous", action="login_failure", subject=f"operator:{username}")
            db.commit()
        raise Unauthenticated("Username or password is incorrect")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/api -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/api/auth.py tests/api/test_hardening.py
git commit -m "fix(api): count unknown-username failures in memory, keep them out of audit_log"
```

---

### Task 3: Audited `unlock-operator` command

**Files:**
- Modify: `selector_drift/api/auth.py` (`_failures`, new `unlock_operator`)
- Modify: `selector_drift/__main__.py` (new subcommand)
- Test: `tests/api/test_hardening.py`

**Interfaces:**
- Produces: `unlock_operator(db: OrmSession, username: str) -> None` (raises `Invalid` if the operator does not exist; writes audit `action="login_unlock"`, `actor="system:unlock-operator"`, `subject="operator:<username>"`; does not commit).
- CLI: `python -m selector_drift unlock-operator <username>` (exit 0 unlocked, 2 unknown or invalid).

`_failures` counts only failures newer than the latest `login_unlock` for the subject. History is never deleted.

- [ ] **Step 1: Write the failing tests**

```python
def test_unlock_clears_the_throttle_and_leaves_the_failure_history(anon_client, operator):
    from selector_drift.api.auth import unlock_operator

    for _ in range(5):
        anon_client.post(LOGIN, json={"username": "op1", "password": "wrong-guess"})
    right = {"username": "op1", "password": operator[1]}
    assert anon_client.post(LOGIN, json=right).status_code == 429
    with Session() as s:
        unlock_operator(s, "op1")
        s.commit()
    assert anon_client.post(LOGIN, json=right).status_code == 200
    with Session() as s:
        failures = s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "login_failure"))
        unlock = s.scalars(select(AuditLog).where(AuditLog.action == "login_unlock")).one()
    assert failures == 5  # evidence kept
    assert unlock.actor == "system:unlock-operator" and unlock.subject == "operator:op1"


def test_failures_after_an_unlock_count_again(anon_client, operator):
    from selector_drift.api.auth import unlock_operator

    with Session() as s:
        unlock_operator(s, "op1")
        s.commit()
    for _ in range(5):
        assert anon_client.post(LOGIN, json={"username": "op1", "password": "wrong-guess"}).status_code == 401
    r = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert r.status_code == 429


def test_unlocking_an_unknown_operator_is_refused(clean_db):
    from selector_drift.api.auth import unlock_operator
    from selector_drift.api.errors import Invalid

    with Session() as s, pytest.raises(Invalid):
        unlock_operator(s, "nobody")


def test_unlock_operator_cli_reports_and_audits(operator, capsys):
    from selector_drift.__main__ import main

    assert main(["unlock-operator", "op1"]) == 0
    assert "op1" in capsys.readouterr().out
    assert latest_audit("login_unlock").subject == "operator:op1"
    assert main(["unlock-operator", "nobody"]) == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/api/test_hardening.py -k unlock -v`
Expected: FAIL with `ImportError: cannot import name 'unlock_operator'`.

- [ ] **Step 3: Implement**

`auth.py`, replace `_failures`:

```python
def _failures(db: OrmSession, username: str, now: datetime) -> tuple[int, datetime | None]:
    subject = f"operator:{username}"
    since = now - THROTTLE_WINDOW
    last_unlock = db.scalar(
        select(func.max(AuditLog.at)).where(
            AuditLog.action == "login_unlock", AuditLog.subject == subject
        )
    )
    if last_unlock is not None and last_unlock > since:
        since = last_unlock
    q = select(func.count(), func.min(AuditLog.at)).where(
        AuditLog.action == "login_failure",
        AuditLog.subject == subject,
        AuditLog.at > since,
    )
    count, oldest = db.execute(q).one()
    return int(count), oldest
```

The comparison changes from `>=` to `>` so a failure at exactly the unlock instant is not counted. Confirm `test_six_failed_logins_throttle_and_survive_a_restart` still passes.

Add below `create_operator`:

```python
def unlock_operator(db: OrmSession, username: str) -> None:
    """Reset the login throttle for one operator. History is kept; the reset is audited."""
    if db.scalars(select(Operator).where(Operator.username == username)).first() is None:
        raise Invalid(f"no such operator: {username}")
    audit(
        db,
        actor="system:unlock-operator",
        action="login_unlock",
        subject=f"operator:{username}",
    )
```

`__main__.py`, add:

```python
def _unlock_operator(username: str) -> int:
    from selector_drift.api.auth import USERNAME_RE, unlock_operator
    from selector_drift.api.errors import Invalid
    from selector_drift.db.session import Session

    if not USERNAME_RE.fullmatch(username):
        print("username must match ^[a-z0-9_.-]{1,64}$", file=sys.stderr)
        return 2
    try:
        with Session() as s:
            unlock_operator(s, username)
            s.commit()
    except Invalid:
        print(f"no such operator: {username}", file=sys.stderr)
        return 2
    print(f"operator {username} unlocked")
    return 0
```

and in `main`:

```python
    unlock = sub.add_parser("unlock-operator", help="reset an operator's login throttle (audited)")
    unlock.add_argument("username")
    ...
    if args.command == "unlock-operator":
        return _unlock_operator(str(args.username))
```

Update the module docstring to mention `unlock-operator`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/api tests/db tests/test_main.py tests/test_import_contracts.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/api/auth.py selector_drift/__main__.py tests/api/test_hardening.py
git commit -m "feat(api): audited unlock-operator command; throttle counts failures since the last unlock"
```

---

### Task 4: Gates, docs and ledger

**Files:**
- Modify: `.superpowers/sdd/2026-10-19-api-task-34/progress.md` (mark the three login items resolved; list the residual below)
- Modify: `docs/superpowers/specs/2026-09-26-selector-drift-api-contract.md` (auth section: verify cap, unknown-user handling, `unlock-operator`)

**Residual risks to record (not fixed here):**
- Per-process cap: N workers allow N x `VERIFY_CAP` verifies. Run one worker or size memory accordingly.
- A flood can keep the cap full and make logins return 429 (the rest of the API is unaffected). Per-client keying needs a trusted proxy and is deferred.
- The 429 on a real username after 5 failures reveals that the username exists. Accepted for a single-operator tool.

- [ ] **Step 1: Run all gates**

Run: `uv run pytest tests/api tests/db tests/test_main.py tests/test_import_contracts.py -q`, `uv run mypy --strict selector_drift`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run lint-imports`
Expected: all green; import contracts 11 kept, 0 broken.

- [ ] **Step 2: Update the ledger and spec, then commit**

```bash
git add .superpowers docs
git commit -m "docs: record login hardening and residual risks"
```

- [ ] **Step 3: Review pass**

Dispatch `ecc:security-reviewer` and `ecc:python-reviewer` on the diff (CLAUDE.md skill map: `api/` is an auth surface). Fix Critical/Important findings, then push.
