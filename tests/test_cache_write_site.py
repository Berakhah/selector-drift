"""C2, structurally: a cache row is constructed in exactly one module."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ALLOWED = {Path("selector_drift/orchestrate/cache_writer.py"), Path("selector_drift/db/models.py")}


def _constructs_cache_row(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "SelectorCacheEntry")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "SelectorCacheEntry")
        )
        for n in ast.walk(tree)
    )


READERS = {Path("selector_drift/resolve/cache.py")}  # may name the model, only inside select()
READ_BUILDERS = {"select", "where", "order_by", "limit", "desc", "asc"}  # query shaping only
WRITE_VERBS = ("insert", "update", "delete", "truncate")
MODEL, TABLE = "SelectorCacheEntry", "selector_cache"
TABLE_HANDLES = {"__table__", "__tablename__"}  # ways to reach the table without naming it


def _names_cache_model(node: ast.AST) -> bool:
    return any(
        (isinstance(n, ast.Name) and n.id == MODEL)
        or (isinstance(n, ast.Attribute) and n.attr == MODEL)
        for n in ast.walk(node)
    )


def _is_built_string(node: ast.AST) -> bool:
    """An f-string, a `+`/`%` concatenation or a .format()/.join() call: SQL assembled from
    parts, where the verb and the table name need not share one constant."""
    return (
        isinstance(node, ast.JoinedStr)
        or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod))
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"format", "join"}
        )
    )


def _str_const(node: ast.AST) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _filled(node: ast.AST) -> str:
    """A `'..'.format(..)` or `'..' % (..)` with its constant arguments put into the template's
    holes. In the source the template precedes its arguments, so reading constants in source
    order cannot see a name split between the template and an argument."""
    try:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Constant)
            and isinstance(node.func.value.value, str)
        ):
            return node.func.value.value.format(
                *map(_str_const, node.args),
                **{k.arg: _str_const(k.value) for k in node.keywords if k.arg},
            )
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            return node.left.value % tuple(map(_str_const, args))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        pass  # a template this cannot fill is still judged by the two spellings below
    return ""


def _text_of(node: ast.AST) -> str:
    """The constants of a built string, spaced (word boundaries kept), run together (a name
    split across parts, 'selector_' + 'cache', reads whole) and, for .format / %, with the
    arguments put into the template's holes, so any of the three spellings is seen."""
    consts = [n for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    consts.sort(key=lambda n: (n.lineno, n.col_offset))  # source order, not BFS order
    parts = [n.value for n in consts]
    filled = "\n".join(_filled(n) for n in ast.walk(node))
    return (" ".join(parts) + "\n" + "".join(parts) + "\n" + filled).lower()


def write_site_offences(path: Path, tree: ast.AST) -> list[str]:
    """Every way, outside the writer and the model, to reach the cache table for a write."""
    out: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call):
            args = [*node.args, *(k.value for k in node.keywords)]
            func = ast.unparse(node.func).rsplit(".", 1)[-1]
            if any(_names_cache_model(a) for a in args) and (
                path not in READERS or func not in READ_BUILDERS
            ):
                out.append(f"{path}:{line} passes the cache model to {func}()")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # An alias hides the model from every name check above, so it is refused
                # everywhere; the reader may import the model, but under its own name.
                if alias.name == MODEL and (path not in READERS or alias.asname is not None):
                    out.append(f"{path}:{line} imports the cache model under an alias or here")
        elif isinstance(node, ast.Attribute) and (
            node.attr in TABLE_HANDLES
            or (node.attr == "tables" and "metadata" in ast.unparse(node))
        ):
            out.append(f"{path}:{line} reaches the cache table via .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.lower()
            if node.value in {MODEL, TABLE}:
                out.append(f"{path}:{line} names the cache model/table by string")
            elif TABLE in text and any(v in text for v in WRITE_VERBS):
                out.append(f"{path}:{line} writes the cache table in SQL text")
        if _is_built_string(node):
            built = _text_of(node)
            if TABLE in built and any(v in built for v in WRITE_VERBS):
                out.append(f"{path}:{line} builds SQL that writes the cache table")
    return out


@pytest.mark.unit
def test_no_other_module_writes_the_cache_table_by_any_spelling() -> None:
    """Construction is one spelling. `insert(SelectorCacheEntry)`, `update(...)`, `delete(...)`,
    `session.merge(...)` and raw SQL are the others (fix round 1, M1); fix round 2 closes the
    bypasses: an import alias of the model, `__table__` / `metadata.tables` access, the model or
    table named by string (getattr, tables["selector_cache"]), and SQL assembled from parts
    (f-string, +, %, .format, .join) whose verb and table name are different constants."""
    offenders: list[str] = []
    for path in sorted(Path("selector_drift").rglob("*.py")):
        if path in ALLOWED:
            continue
        offenders += write_site_offences(path, ast.parse(path.read_text(encoding="utf-8")))
    assert offenders == []


PLANTED = {
    "import alias": "from selector_drift.db.models import SelectorCacheEntry as Entry\n",
    "module import": "from selector_drift.db.models import SelectorCacheEntry\n",
    "__table__": "def f(m):\n    return m.__table__\n",
    "__tablename__": "def f(m):\n    return m.__tablename__\n",
    "metadata.tables": "def f(Base):\n    return Base.metadata.tables['t']\n",
    "table by string": "def f(m):\n    return m['selector_cache']\n",
    "model by string": "def f(m):\n    return getattr(m, 'SelectorCacheEntry')\n",
    "f-string": "def f(s, t):\n    s.execute(f'INSERT INTO selector_cache {t}')\n",
    "concatenation": "def f(s):\n    s.execute('remove from ' + 'selector_cache' + ' -- delete')\n",
    "format": "def f(s):\n    s.execute('update {} set x=1'.format('selector_cache'))\n",
    "join": "def f(s):\n    s.execute(' '.join(['truncate', 'selector_cache']))\n",
}

# The rule each by-string case must be flagged by (the whole-constant rules; the first two
# cases hold no verb, so the SQL-text and built-string rules cannot be what fires).
BY_STRING_RULE = "names the cache model/table by string"
BY_STRING = ("table by string", "model by string")

# The table name itself is split, so no constant -- and no whole-constant rule -- ever sees it.
BUILT_ONLY = {
    "split name by +": "def f(s):\n    s.execute('insert into ' + 'selector_' + 'cache')\n",
    "split name by f-string": (
        "def f(s):\n    s.execute(f'insert into selector_{\"cache\"} values (1)')\n"
    ),
    "split name by %": "def f(s):\n    s.execute('delete from %s%s' % ('selector_', 'cache'))\n",
    "split name by format": (
        "def f(s):\n    s.execute('update {}{} set x=1'.format('selector_', 'cache'))\n"
    ),
    "split name by join": "def f(s):\n    s.execute(''.join(['truncate selector_', 'cache']))\n",
    "variable between the parts": (
        "def f(s, name):\n    s.execute('insert into ' + name + 'selector_' + 'cache')\n"
    ),
    # The template precedes its arguments in the source, but they fill its holes in the text.
    "template before its format argument": (
        "def f(s):\n    s.execute('insert into {}cache'.format('selector_'))\n"
    ),
    "template before its % argument": (
        "def f(s):\n    s.execute('delete from %scache' % ('selector_',))\n"
    ),
}
BUILT_RULE = "builds SQL that writes the cache table"


@pytest.mark.unit
@pytest.mark.parametrize("source", BUILT_ONLY.values(), ids=BUILT_ONLY.keys())
def test_only_the_built_string_rule_can_flag_a_name_assembled_from_parts(source: str) -> None:
    """No constant here holds the table name, so the whole-constant rules are blind to it;
    only `_is_built_string` can fire, and the offence must say so."""
    tree = ast.parse(source)
    assert not any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and TABLE in n.value.lower()
        for n in ast.walk(tree)
    ), "a whole-constant rule could fire: the case is not isolated"
    offences = write_site_offences(Path("selector_drift/orchestrate/planted.py"), tree)
    assert offences != []
    assert all(BUILT_RULE in o for o in offences), offences


@pytest.mark.unit
@pytest.mark.parametrize("case", BY_STRING)
def test_by_string_rule_is_isolated_and_named(case: str) -> None:
    """The model/table-by-string cases carry no verb and no assembly, so the SQL-text and
    built-string rules cannot be what flags them: exactly this rule is."""
    offences = write_site_offences(
        Path("selector_drift/orchestrate/planted.py"), ast.parse(PLANTED[case])
    )
    assert len(offences) == 1
    assert BY_STRING_RULE in offences[0]


@pytest.mark.unit
@pytest.mark.parametrize("source", PLANTED.values(), ids=PLANTED.keys())
def test_each_bypass_rule_fails_on_a_planted_violation(source: str) -> None:
    """A guard nobody has seen fail is not a guard: every rule is shown refusing its own
    planted violation, in a module that is neither the writer nor the model."""
    planted = Path("selector_drift/orchestrate/planted.py")
    assert write_site_offences(planted, ast.parse(source)) != []


@pytest.mark.unit
def test_the_reader_may_import_the_model_but_not_alias_it() -> None:
    reader = next(iter(READERS))
    plain = ast.parse("from selector_drift.db.models import SelectorCacheEntry\n")
    aliased = ast.parse("from selector_drift.db.models import SelectorCacheEntry as E\n")
    assert write_site_offences(reader, plain) == []
    assert write_site_offences(reader, aliased) != []


@pytest.mark.unit
def test_only_the_cache_writer_constructs_a_cache_row() -> None:
    """A second write site is a second way to spell 'write the cache' without first spelling
    'the gate passed' -- the one ordering C2 exists to make unspellable."""
    offenders = sorted(
        str(p)
        for p in Path("selector_drift").rglob("*.py")
        if p not in ALLOWED and _constructs_cache_row(p)
    )
    assert offenders == []
