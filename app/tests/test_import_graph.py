"""Reverse import edges — the closest thing this pipeline has to impact analysis.

Nothing here computes whether editing a file breaks a CALLER of it; ``agents/code_review.py``
documents that as a known gap. It does not matter when generating a new app (every file is new, so
there are no pre-existing callers) and it is the central risk when changing an existing one, where
a two-line edit to a shared helper can break code the run never looked at.

This is file-level import edges, not a call graph, and the tests pin exactly that scope: what it
answers, and what it deliberately does not.

Pure functions over ``{path: content}`` — no executor, no LLM.
"""

from __future__ import annotations

import pytest

from app.services.wiring import build_import_graph, dependents_of

_FILES = {
    # Python: relative and absolute imports, and a third-party one that must be ignored.
    "src/auth/__init__.py": "",
    "src/auth/token.py": "import os\nimport jwt\n",
    "src/auth/login.py": "from .token import verify\nfrom src.db import conn\n",
    "src/api/routes.py": "from src.auth.login import login\n",
    "src/db.py": "x = 1\n",
    # JS: relative requires and a bare package import that must be ignored.
    "web/app.js": "const r = require('./routes');\n",
    "web/routes.js": "import express from 'express';\nconst h = require('../util/h');\n",
    "util/h.js": "module.exports = {};\n",
    # Not source at all.
    "README.md": "# docs\n",
}


def test_python_relative_and_absolute_imports_both_produce_edges() -> None:
    graph = build_import_graph(_FILES)
    assert graph["src/auth/token.py"] == {"src/auth/login.py"}      # from .token
    assert graph["src/db.py"] == {"src/auth/login.py"}              # from src.db
    assert graph["src/auth/login.py"] == {"src/api/routes.py"}


def test_javascript_relative_imports_produce_edges() -> None:
    graph = build_import_graph(_FILES)
    assert graph["util/h.js"] == {"web/routes.js"}                  # ../util/h -> util/h.js
    assert graph["web/routes.js"] == {"web/app.js"}                 # ./routes  -> web/routes.js


def test_third_party_packages_are_not_edges() -> None:
    # `import jwt` / `from express` have no in-set target; they are a dependency question, which
    # reconcile_package_dependencies answers, not a "who depends on this file" question.
    graph = build_import_graph(_FILES)
    assert "jwt" not in graph and "express" not in graph


def test_every_file_is_a_key_even_with_no_dependents() -> None:
    # So a caller can distinguish "nothing imports this" from "I never looked at this".
    graph = build_import_graph(_FILES)
    assert set(graph) == set(_FILES)
    assert graph["src/api/routes.py"] == set()      # a leaf: nothing imports it
    assert graph["README.md"] == set()              # not source: contributes a key, no edges


def test_dependents_are_direct_by_default_and_transitive_on_request() -> None:
    graph = build_import_graph(_FILES)
    assert dependents_of(graph, ["src/auth/token.py"]) == ["src/auth/login.py"]
    assert dependents_of(graph, ["src/auth/token.py"], depth=2) == [
        "src/api/routes.py", "src/auth/login.py",
    ]


def test_the_seed_files_are_excluded_from_their_own_blast_radius() -> None:
    # The question is "what ELSE does this change touch", so a target is never its own dependent.
    graph = build_import_graph(_FILES)
    got = dependents_of(graph, ["src/auth/token.py", "src/auth/login.py"], depth=3)
    assert "src/auth/token.py" not in got and "src/auth/login.py" not in got
    assert got == ["src/api/routes.py"]


def test_commented_out_imports_do_not_count() -> None:
    # A commented import is not a dependency; counting it would invent a caller that is not there.
    py = build_import_graph({"a.py": "# from .b import x\n", "b.py": ""})
    js = build_import_graph({"a.js": "// const b = require('./b');\n", "b.js": ""})
    assert py["b.py"] == set() and js["b.js"] == set()


def test_a_package_init_import_resolves_to_the_init_file() -> None:
    files = {"pkg/__init__.py": "", "pkg/thing.py": "", "main.py": "from pkg import thing\n"}
    assert build_import_graph(files)["pkg/__init__.py"] == {"main.py"}


def test_self_imports_are_not_edges() -> None:
    assert build_import_graph({"a.py": "import a\n"})["a.py"] == set()


def test_a_cycle_terminates() -> None:
    # Mutual imports are legal Python; a naive traversal would loop forever.
    files = {"a.py": "from . import b\n", "b.py": "from . import a\n", "__init__.py": ""}
    graph = build_import_graph(files)
    assert dependents_of(graph, ["a.py"], depth=5) == ["b.py"]


def test_an_unparseable_file_does_not_sink_the_survey() -> None:
    # A brownfield repo may contain Python this interpreter cannot parse (a different target
    # version), which is exactly why extraction is regex-based rather than ast-based.
    files = {"broken.py": "def f(:\n", "ok.py": "from .dep import x\n", "dep.py": ""}
    graph = build_import_graph(files)
    assert graph["dep.py"] == {"ok.py"}       # the good file was still surveyed


def test_an_empty_file_set_is_an_empty_graph() -> None:
    assert build_import_graph({}) == {}
    assert dependents_of({}, ["anything.py"]) == []


def test_from_package_import_module_is_an_edge() -> None:
    """``from . import routes`` names the MODULE in the imported-names clause, not the module
    clause. Missing this shape silently under-reports dependents — and an impact report claiming
    "nothing imports this" when something does is worse than having no report at all."""
    files = {
        "pkg/__init__.py": "",
        "pkg/routes.py": "",
        "pkg/app.py": "from . import routes\n",
        "main.py": "from pkg import routes\n",
    }
    assert build_import_graph(files)["pkg/routes.py"] == {"pkg/app.py", "main.py"}


def test_aliased_and_parenthesised_imports_are_handled() -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "",
        "pkg/b.py": "",
        "pkg/use.py": "from . import (a, b as bee)\n",
    }
    graph = build_import_graph(files)
    assert graph["pkg/a.py"] == {"pkg/use.py"}
    assert graph["pkg/b.py"] == {"pkg/use.py"}


def test_star_import_still_links_the_package() -> None:
    files = {"pkg/__init__.py": "", "pkg/use.py": "from . import *\n"}
    # `*` binds no module name, but the package itself is still imported.
    assert build_import_graph(files)["pkg/__init__.py"] == {"pkg/use.py"}


def test_importing_a_function_from_a_module_does_not_invent_a_file() -> None:
    # `from src.db import conn` emits the speculative specifier `src.db.conn`; it must resolve to
    # nothing rather than fabricating an edge.
    files = {"src/db.py": "conn = 1\n", "src/use.py": "from src.db import conn\n"}
    graph = build_import_graph(files)
    assert graph["src/db.py"] == {"src/use.py"}
    assert set(graph) == set(files)          # no phantom "src/db/conn.py" key appeared


# --- shapes found by adversarial review ---------------------------------------------------------


def test_multiline_parenthesised_from_import_is_not_missed() -> None:
    """``from . import (\n a,\n b,\n)`` is how a package barrel is usually imported, and the
    names group cannot cross a newline unless the parenthesised form is matched explicitly. Missing
    it under-reports dependents exactly where the most-imported file lives."""
    files = {
        "pkg/__init__.py": "",
        "pkg/routes.py": "",
        "pkg/users.py": "",
        "pkg/app.py": "from . import (\n    routes,\n    users,\n)\n",
    }
    graph = build_import_graph(files)
    assert graph["pkg/routes.py"] == {"pkg/app.py"}
    assert graph["pkg/users.py"] == {"pkg/app.py"}


@pytest.mark.parametrize(
    "content",
    [
        '"""Usage:\nfrom helper import thing\n"""\n',      # a usage example in a module docstring
        'DOC = """\nimport helper\n"""\n',                  # an assigned triple-quoted string
        "'''Usage:\nimport helper\n'''\n",                  # single-quoted triple
        '"""import helper"""\n',                            # one-line docstring
    ],
)
def test_an_import_inside_a_docstring_does_not_invent_a_caller(content: str) -> None:
    # Under-reporting a dependent is bad; ASSERTING one that does not exist is worse, because it is
    # indistinguishable from a true one in the blast-radius table a reviewer relies on.
    graph = build_import_graph({"a.py": content, "helper.py": ""})
    assert graph["helper.py"] == set(), "a docstring example was counted as a real import"


def test_real_imports_after_a_docstring_are_still_found() -> None:
    # The docstring handling must not swallow the code that follows it.
    one_line = build_import_graph({"a.py": '"""Doc."""\nfrom .helper import thing\n', "helper.py": ""})
    assert one_line["helper.py"] == {"a.py"}

    multi = build_import_graph({"a.py": '"""Doc\nspanning lines\n"""\nimport helper\n', "helper.py": ""})
    assert multi["helper.py"] == {"a.py"}
