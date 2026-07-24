"""Deterministic wiring reconciliation (app/services/wiring.py).

Pins issue 2a's fix: an Express app factory that ships its module routers commented out gets them
mounted, deterministically and idempotently, from the generated router files alone.
"""

from __future__ import annotations

import json

from app.services.wiring import (
    reconcile_express_routers,
    reconcile_package_peerdeps,
    reconcile_wiring,
)

# The exact shape the isolated app-factory item emits: express() + a commented placeholder, routers
# never mounted.
APP_JS = """const express = require('express');

function createApp() {
  const app = express();
  app.use(express.json());

  // Module routers
  // app.use('/api/module', require('./modules/module/module.router'));

  // 404 handler
  app.use((req, res, next) => {
    res.status(404).json({ error: 'Not Found' });
  });

  return app;
}

module.exports = createApp;
"""

FILES = {
    "src/app.js": APP_JS,
    "src/server.js": "const createApp = require('./app');\ncreateApp().listen(3000);\n",
    "src/modules/orders/orders.routes.js": "const r = require('express').Router();\nmodule.exports = r;\n",
    "src/modules/users/users.routes.js": "const r = require('express').Router();\nmodule.exports = r;\n",
    "src/modules/orders/orders.routes.test.js": "test('x', () => {});\n",  # ignored (test)
}


def test_routers_are_mounted_in_the_app_factory() -> None:
    changed = reconcile_express_routers(FILES)
    assert set(changed) == {"src/app.js"}
    out = changed["src/app.js"]
    assert "app.use('/api/orders', require('./modules/orders/orders.routes'));" in out
    assert "app.use('/api/users', require('./modules/users/users.routes'));" in out
    # The commented placeholder is removed; the test-file router is NOT mounted.
    assert "/api/module" not in out
    assert "orders.routes.test" not in out
    # Mounted before the 404 catch-all so real routes win.
    assert out.index("/api/orders") < out.index("404 handler")


def test_reconcile_is_idempotent() -> None:
    once = reconcile_express_routers(FILES)
    files2 = {**FILES, **once}
    assert reconcile_express_routers(files2) == {}   # second pass changes nothing


def test_already_wired_router_is_not_duplicated() -> None:
    wired = dict(FILES)
    wired["src/app.js"] = APP_JS.replace(
        "  // app.use('/api/module', require('./modules/module/module.router'));",
        "  app.use('/api/orders', require('./modules/orders/orders.routes'));",
    )
    changed = reconcile_express_routers(wired)
    # orders already mounted → only users is added, and orders is not mounted twice.
    out = changed["src/app.js"]
    assert out.count("/api/orders'") == 1
    assert "/api/users" in out


def test_no_entry_file_is_a_noop() -> None:
    assert reconcile_express_routers({"src/modules/x/x.routes.js": "module.exports = {};\n"}) == {}


def test_esm_entry_is_left_untouched() -> None:
    esm = {
        "src/app.js": "import express from 'express';\nconst app = express();\nexport default app;\n",
        "src/modules/x/x.routes.js": "export default {};\n",
    }
    assert reconcile_express_routers(esm) == {}      # ESM not handled in v1 → reported, not rewritten


def test_reconcile_wiring_runs_both_fixers() -> None:
    files = {**FILES, "package.json": json.dumps({
        "devDependencies": {"eslint": "^9.0.0", "eslint-plugin-react-hooks": "^4.6.0"},
    })}
    changed = reconcile_wiring(files)
    assert "src/app.js" in changed                     # router fixer ran
    assert "package.json" in changed                    # peer-dep fixer ran


# --- peer-dep fixer (issue 6a) -----------------------------------------------

def test_eslint9_bumps_incompatible_react_hooks_plugin() -> None:
    pkg = {"devDependencies": {"eslint": "^9.2.0", "eslint-plugin-react-hooks": "^4.6.0"}}
    changed = reconcile_package_peerdeps({"package.json": json.dumps(pkg)})
    out = json.loads(changed["package.json"])
    assert out["devDependencies"]["eslint-plugin-react-hooks"] == "^5.1.0"


def test_peerdep_fixer_is_idempotent_and_scoped() -> None:
    # Already compatible (eslint 8) -> untouched; and re-running the bumped file is a no-op.
    compatible = {"package.json": json.dumps(
        {"devDependencies": {"eslint": "^8.57.0", "eslint-plugin-react-hooks": "^4.6.0"}})}
    assert reconcile_package_peerdeps(compatible) == {}
    bumped = {"package.json": json.dumps(
        {"devDependencies": {"eslint": "^9.2.0", "eslint-plugin-react-hooks": "^5.1.0"}})}
    assert reconcile_package_peerdeps(bumped) == {}
