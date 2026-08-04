"""The JS test harness is DETERMINISTIC scaffold output, not model output.

Regression tests for a shipped full-stack repo (Node/Express + React/Vite, tested with Jest) whose
``jest.config.js`` was written by the LLM inside a BACKEND-framed work item. Blind to the React
frontend sharing that config, the model emitted ``testEnvironment: 'node'`` and a testMatch that
only caught ``*.test.js``, and no Babel config at all — so 103 of 235 generated test files were
never collected, every JSX/ESM test failed to parse, and dynamic ``import()`` died on
"A dynamic import callback was invoked without --experimental-vm-modules".

Two halves, and BOTH are needed — either alone leaves the bug in place:

  1. ``boilerplate.render_scaffold`` emits the harness (jest.config.cjs, babel.config.cjs,
     jest.setup.cjs, test-utils/) with the shape the capabilities imply.
  2. ``plan_builder`` keeps those files OUT of every work item's ``target_files``, because
     ``scaffold_node`` runs FIRST and the code generator would otherwise overwrite the good file.
"""

from __future__ import annotations

import json

from app.models import WorkItem
from app.services.boilerplate import render_scaffold
from app.services.plan_builder import (
    _is_scaffold_owned,
    _missing_after_reconcile,
    _source_leaves,
)

# Shared-root Node/React pack: ONE package.json, ONE jest config serving backend AND frontend —
# the exact shape that shipped broken.
#
# Carries a LEGACY-shaped (rich_api_mapping) mapping CSV on purpose: app.services.plan_builder's
# ADAPTIVE path (no rich_api_mapping present) unconditionally re-roots every generated file under
# canonical backend/ / frontend/ folders regardless of what these trees are named or wrapped in
# (see plan_builder._reroot) — so an adaptive-shaped version of this fixture would never actually
# share a root with plan_builder's real output, and app.services.boilerplate now detects that via
# design_pack.has_rich_api_mapping() and forces backend/ + frontend/ roots to match. Only the
# LEGACY path keeps paths byte-for-byte, so only a legacy-shaped pack can genuinely reach the
# "shared root, one combined manifest, one jsdom jest config" scenario this file protects.
SHARED_ROOT_PACK = {
    "backend-structure.json": {
        "tree": {
            "src/": {"server.js": "http entrypoint", "app.js": "express app factory"},
            "package.json": "devDeps: jest, supertest",
            "jest.config.js": "Jest config: test environment, coverage thresholds, module aliases",
        }
    },
    "frontend-structure.json": {
        "tree": {"src/": {"App.jsx": "root component", "main.jsx": "entry"}}},
    "api-to-ui-mapping.csv": "operation_id,endpoint_path,req_ids\nloginUser,/api/login,REQ-1\n",
}

# Separated pack: the backend is Node (jest), the frontend is its own Vite/vitest project. No
# rich_api_mapping CSV, so this goes through the ADAPTIVE path — which re-roots both sides under
# canonical backend/ / frontend/ regardless of the "quickbite-..." wrapper names these trees use
# (plan_builder._reroot discards wrapper names outright), so that is what the scaffold must place
# its manifests/harness under too — see design_pack.has_rich_api_mapping / boilerplate.py.
SEPARATED_PACK = {
    "backend-structure.json": {
        "tree": {"quickbite-backend/": {"src/": {"server.js": "entry", "app.js": "factory"}}}},
    "frontend-structure.json": {
        "tree": {"quickbite-frontend/": {"src/": {"App.jsx": "root"}}}},
}


def _by_path(project: str, pack: dict) -> dict[str, str]:
    return {f["path"]: f["content"] for f in render_scaffold(project, pack)}


# --- the harness exists, and only where jest is actually the runner -----------------------------

def test_shared_root_node_react_pack_emits_the_whole_harness() -> None:
    files = _by_path("quickbite", SHARED_ROOT_PACK)

    for path in (
        "jest.config.cjs",
        "babel.config.cjs",
        "jest.setup.cjs",
        "test-utils/fileMock.cjs",
        "test-utils/styleMock.cjs",
    ):
        assert path in files, f"{path} was not scaffolded"
        assert files[path].strip()


def test_config_files_are_cjs_not_js() -> None:
    # A generated package.json commonly declares "type": "module"; a .js config is then loaded as
    # an ES module and `module.exports` throws before a single test runs.
    files = _by_path("quickbite", SHARED_ROOT_PACK)

    assert "jest.config.js" not in files
    assert "babel.config.js" not in files
    assert "module.exports" in files["jest.config.cjs"]
    assert "module.exports" in files["babel.config.cjs"]


def test_vitest_frontend_with_python_backend_gets_no_jest_harness() -> None:
    # The legacy default stack (FastAPI + React/Vite) runs vitest, so scaffolding a jest harness
    # would ship config for a runner the project never invokes.
    files = _by_path("acme", {})

    assert not [p for p in files if "jest" in p or "babel" in p]


# --- the shape that was wrong ---------------------------------------------------------------

def test_frontend_sharing_the_root_makes_the_environment_jsdom() -> None:
    config = _by_path("quickbite", SHARED_ROOT_PACK)["jest.config.cjs"]
    assert "testEnvironment: 'jsdom'" in config
    assert "testEnvironment: 'node'" not in config


def test_backend_only_root_stays_on_the_node_environment() -> None:
    # Separated pack: the backend project root has no React in it, so jsdom would be dead weight.
    # No rich_api_mapping CSV -> the adaptive path re-roots both sides to canonical backend/ /
    # frontend/, discarding the "quickbite-..." wrapper names the input trees used.
    files = _by_path("quickbite", SEPARATED_PACK)

    assert "backend/jest.config.cjs" in files
    assert "frontend/jest.config.cjs" not in files  # that side runs vitest
    assert "testEnvironment: 'node'" in files["backend/jest.config.cjs"]


def test_testmatch_catches_jsx_ts_and_tsx_not_only_js() -> None:
    # The silent killer: a '**/*.test.js'-only testMatch collects nothing for a .jsx/.tsx suite and
    # still reports green.
    config = _by_path("quickbite", SHARED_ROOT_PACK)["jest.config.cjs"]

    assert "'**/__tests__/**/*.test.[jt]s?(x)'" in config
    assert "'**/*.test.[jt]s?(x)'" in config
    assert "'**/*.spec.[jt]s?(x)'" in config
    assert "testPathIgnorePatterns: ['/node_modules/', '/dist/', '/build/']" in config


def test_asset_mapping_precedes_the_at_alias() -> None:
    # Jest applies the FIRST moduleNameMapper pattern that matches, and '@/assets/logo.svg' matches
    # BOTH — alias-first means Jest hands the real SVG to the JS parser.
    config = _by_path("quickbite", SHARED_ROOT_PACK)["jest.config.cjs"]

    # Compare the mapper KEYS (quoted, colon-terminated) — the prose above them names the alias too.
    assert config.index("(svg|png|jpe?g|gif|webp|avif|ico)$':") < config.index("'^@/(.*)$':")
    assert "<rootDir>/test-utils/fileMock.cjs" in config


def test_setup_file_is_wired_through_setup_files_after_env() -> None:
    config = _by_path("quickbite", SHARED_ROOT_PACK)["jest.config.cjs"]
    assert "setupFilesAfterEnv: ['<rootDir>/jest.setup.cjs']" in config


# --- babel ---------------------------------------------------------------------------------

def test_babel_adds_the_react_preset_only_when_jsx_shares_the_root() -> None:
    shared = _by_path("quickbite", SHARED_ROOT_PACK)["babel.config.cjs"]
    backend_only = _by_path("quickbite", SEPARATED_PACK)["backend/babel.config.cjs"]

    assert "@babel/preset-env" in shared and "@babel/preset-react" in shared
    assert "runtime: 'automatic'" in shared
    assert "@babel/preset-env" in backend_only
    assert "@babel/preset-react" not in backend_only


def test_babel_rewrites_import_meta_which_preset_env_leaves_alone() -> None:
    babel = _by_path("quickbite", SHARED_ROOT_PACK)["babel.config.cjs"]

    assert "MetaProperty" in babel
    assert "pathToFileURL" in babel
    # module.filename, NOT a bare __filename: Babel renames __filename identifiers it did not
    # introduce, producing a self-referential expression.
    assert "t.identifier('module')" in babel and "t.identifier('filename')" in babel
    assert "__filename" not in babel.replace("`__filename`", "")


# --- setup polyfills ------------------------------------------------------------------------

def test_setup_polyfills_are_guarded_and_cover_the_known_gaps() -> None:
    setup = _by_path("quickbite", SHARED_ROOT_PACK)["jest.setup.cjs"]

    # Each polyfill sits behind its OWN typeof guard, so it fills a gap and never shadows a real
    # implementation (a newer Node, jsdom, or a deliberate test double).
    for symbol in (
        "setImmediate",
        "clearImmediate",
        "TextEncoder",
        "TextDecoder",
        "MessagePort",
        "ReadableStream",
        "Response",
        "fetch",
    ):
        assert f"typeof globalThis.{symbol} === 'undefined'" in setup, f"{symbol} not guarded"
    assert "typeof globalThis.Element.prototype.scrollIntoView === 'undefined'" in setup
    assert "require('worker_threads')" in setup      # MessagePort, for undici at require time
    assert "require('stream/web')" in setup          # ReadableStream
    assert "require('@testing-library/jest-dom')" in setup


def test_backend_only_setup_skips_the_dom_pieces() -> None:
    setup = _by_path("quickbite", SEPARATED_PACK)["backend/jest.setup.cjs"]

    assert "@testing-library/jest-dom" not in setup
    assert "scrollIntoView" not in setup
    assert "MessagePort" in setup  # the require-time crash is not a DOM problem


def test_file_and_style_mocks_have_the_right_shape() -> None:
    files = _by_path("quickbite", SHARED_ROOT_PACK)

    assert "module.exports = 'test-file-stub';" in files["test-utils/fileMock.cjs"]
    # An object, so `styles.button` from a CSS module reads undefined instead of throwing.
    assert "module.exports = {};" in files["test-utils/styleMock.cjs"]


# --- the harness's own dependencies ----------------------------------------------------------

def test_harness_devdependencies_land_in_the_manifest() -> None:
    pkg = json.loads(_by_path("quickbite", SHARED_ROOT_PACK)["package.json"])
    dev = pkg["devDependencies"]

    for name in (
        "jest",
        "babel-jest",
        "@babel/core",
        "@babel/preset-env",
        "@babel/preset-react",
        "jest-environment-jsdom",
        "@testing-library/jest-dom",
    ):
        assert name in dev, f"{name} missing from devDependencies"


def test_babel_packages_are_pinned_to_the_7_line() -> None:
    # babel-jest peer-depends on @babel/core ^7; letting ^8 resolve makes npm install ERESOLVE.
    dev = json.loads(_by_path("quickbite", SHARED_ROOT_PACK)["package.json"])["devDependencies"]

    for name in ("@babel/core", "@babel/preset-env", "@babel/preset-react"):
        assert dev[name].startswith("^7."), f"{name} must stay on the ^7 line, got {dev[name]}"


def test_backend_only_manifest_skips_the_dom_packages() -> None:
    dev = json.loads(
        _by_path("quickbite", SEPARATED_PACK)["backend/package.json"]
    )["devDependencies"]

    assert "babel-jest" in dev and "@babel/preset-env" in dev
    assert "jest-environment-jsdom" not in dev
    assert "@babel/preset-react" not in dev


def test_harness_rendering_is_deterministic() -> None:
    assert render_scaffold("quickbite", SHARED_ROOT_PACK) == render_scaffold("quickbite", SHARED_ROOT_PACK)


# --- the plan must not target what the scaffold owns -------------------------------------------

_TREE_WITH_HARNESS = {
    "src/": {"app.js": "express app", "server.js": "http entrypoint"},
    "package.json": "manifest",
    "jest.config.js": "Jest config: test environment, coverage thresholds, module aliases",
    "babel.config.js": "Babel presets",
    "jest.setup.js": "global test setup",
}


def test_scaffold_owned_matcher_is_name_based_not_extension_based() -> None:
    for path in ("jest.config.js", "jest.config.cjs", "jest.config.ts", "babel.config.mjs",
                 "jest.setup.js", ".babelrc", ".babelrc.json", "src/jest.config.js"):
        assert _is_scaffold_owned(path), f"{path} should be scaffold-owned"
    # Near-misses must still be generated normally.
    for path in ("vite.config.js", "eslint.config.js", "knexfile.js", "src/config.js",
                 "jestconfig.js", "src/jest.setup.helper.js"):
        assert not _is_scaffold_owned(path), f"{path} must stay a work-item target"


def test_harness_files_are_not_planned_as_work_item_targets() -> None:
    planned = {p for p, _ in _source_leaves(_TREE_WITH_HARNESS)}

    assert planned == {"src/app.js", "src/server.js", "package.json"}
    # package.json overlap is deliberately left alone here — a separate change owns it.
    assert "package.json" in planned


def test_excluded_harness_files_are_not_reported_as_uncovered() -> None:
    """The 'every structure leaf is produced' invariant stays honest: the harness files are still
    produced, by ``scaffold_node`` rather than by a work item, so they must not read as missing."""
    items = [WorkItem(id="backend-root", target_files=["src/app.js", "src/server.js", "package.json"])]

    assert _missing_after_reconcile(_TREE_WITH_HARNESS, {}, items) == []


def test_every_excluded_leaf_has_a_scaffolded_counterpart() -> None:
    """Coherence between the two halves: what the plan drops, the scaffold really does emit."""
    dropped = {p for p in _TREE_WITH_HARNESS if _is_scaffold_owned(p)}
    assert dropped == {"jest.config.js", "babel.config.js", "jest.setup.js"}

    scaffolded = set(_by_path("quickbite", SHARED_ROOT_PACK))
    for path in dropped:
        stem = path.rsplit(".", 1)[0]          # jest.config.js -> jest.config
        assert f"{stem}.cjs" in scaffolded, f"nothing scaffolded for the dropped {path}"
