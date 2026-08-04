"""Deterministic wiring reconciliation (app/services/wiring.py).

Pins issue 2a's fix: an Express app factory that ships its module routers commented out gets them
mounted, deterministically and idempotently, from the generated router files alone.

Also pins the two manifest/import gaps that per-item isolation creates: packages the generated code
imports but no package.json declares (the ten ``npm install`` rescues a real run needed), and
relative imports pointing at modules nobody generated — the first fixed, the second only reported.
"""

from __future__ import annotations

import json

from app.services.wiring import (
    _NPM_VERSIONS,
    find_unresolved_imports,
    reconcile_express_routers,
    reconcile_package_dependencies,
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


# --- dependency reconciliation (the ten `npm install` rescues) ---------------------------------

# A backend whose manifest knows only about express, while the generated code imports the packages
# the real run had to install by hand. Deliberately mixes require/import forms, node builtins, a
# relative import, a subpath import and a scoped package.
PKG_JSON = json.dumps(
    {"name": "api", "version": "1.0.0", "dependencies": {"express": "^4.19.2"}}, indent=2
) + "\n"

BACKEND = {
    "package.json": PKG_JSON,
    "src/app.js": (
        "const express = require('express');\n"
        "const helmet = require('helmet');\n"
        "const cors = require('cors');\n"
        "const compression = require('compression');\n"
        "const path = require('path');\n"                       # builtin -> never a dependency
        "const routes = require('./routes');\n"                 # relative -> never a package
        "const app = express();\n"
    ),
    "src/routes/index.js": "module.exports = require('express').Router();\n",
    "src/db.js": "const { Pool } = require('pg');\nmodule.exports = new Pool();\n",
    "src/auth.js": (
        "import bcrypt from 'bcrypt';\n"
        "import jwt from 'jsonwebtoken';\n"
        "import { body } from 'express-validator';\n"
        "import { randomUUID } from 'node:crypto';\n"           # prefixed builtin
        "import { readFile } from 'fs/promises';\n"             # builtin subpath
        "import debounce from 'lodash/debounce';\n"             # subpath -> 'lodash'
        "import { Client } from '@elastic/elasticsearch/lib/client';\n"  # scoped subpath
    ),
    "src/app.test.js": "const request = require('supertest');\nconst app = require('./app');\n",
}


def _deps(changed: dict[str, str], path: str = "package.json") -> dict[str, dict[str, str]]:
    pkg = json.loads(changed[path])
    return {"dependencies": pkg.get("dependencies", {}), "devDependencies": pkg.get("devDependencies", {})}


def test_missing_runtime_dependencies_are_declared() -> None:
    changed = reconcile_package_dependencies(BACKEND)
    deps = _deps(changed)["dependencies"]
    for name in ("pg", "bcrypt", "jsonwebtoken", "express-validator", "helmet", "cors", "compression"):
        assert deps[name] == _NPM_VERSIONS[name], name       # curated range, never a guessed pin
    assert deps["express"] == "^4.19.2"                       # the already-declared one is untouched


def test_test_only_imports_go_to_dev_dependencies() -> None:
    changed = reconcile_package_dependencies(BACKEND)
    sections = _deps(changed)
    assert "supertest" in sections["devDependencies"]          # imported only by src/app.test.js
    assert "supertest" not in sections["dependencies"]
    assert "pg" not in sections["devDependencies"]             # a runtime import stays runtime


def test_relative_and_builtin_imports_are_never_packages() -> None:
    deps = _deps(reconcile_package_dependencies(BACKEND))
    declared = {**deps["dependencies"], **deps["devDependencies"]}
    for never in ("./routes", "routes", "./app", "app", "path", "crypto", "node:crypto", "fs", "fs/promises"):
        assert never not in declared, never


def test_subpath_and_scoped_imports_reduce_to_the_package_name() -> None:
    deps = _deps(reconcile_package_dependencies(BACKEND))["dependencies"]
    assert "lodash" in deps and "lodash/debounce" not in deps
    assert "@elastic/elasticsearch" in deps
    assert "@elastic/elasticsearch/lib/client" not in deps


def test_dependency_fixer_is_idempotent() -> None:
    once = reconcile_package_dependencies(BACKEND)
    assert reconcile_package_dependencies({**BACKEND, **once}) == {}


def test_reconcile_wiring_is_idempotent_end_to_end() -> None:
    files = {**FILES, "package.json": PKG_JSON}
    once = reconcile_wiring(files)
    assert once                                                # something was fixed on pass 1
    assert reconcile_wiring({**files, **once}) == {}           # pass 2 changes nothing


def test_no_manifest_means_no_change() -> None:
    assert reconcile_package_dependencies({"src/a.js": "require('pg');\n"}) == {}


def test_already_declared_dependency_is_not_moved_or_duplicated() -> None:
    files = {
        "package.json": json.dumps({"devDependencies": {"axios": "^1.0.0"}}),
        "src/a.js": "import axios from 'axios';\n",            # runtime use of a dev-declared package
    }
    assert reconcile_package_dependencies(files) == {}         # declared anywhere == declared


def test_project_aliases_and_self_resolving_specifiers_are_not_packages() -> None:
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "tsconfig.json": '{\n  // path aliases\n  "compilerOptions": {"paths": {"@app/*": ["src/*"]}},\n}',
        "src/main.ts": (
            "import App from '@/components/App';\n"            # '@/...' is never an npm package
            "import { store } from '@app/store';\n"            # tsconfig alias (jsonc + trailing comma)
            "import { api } from 'utils/api';\n"               # resolves to src/utils/api.ts
        ),
        "src/utils/api.ts": "export const api = {};\n",
    }
    assert reconcile_package_dependencies(files) == {}


def test_unknown_bare_package_falls_back_to_star_and_unknown_scope_is_skipped() -> None:
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "src/a.js": "require('some-unlisted-lib');\nrequire('@team/widgets');\nrequire('AppError');\n",
    }
    deps = _deps(reconcile_package_dependencies(files))["dependencies"]
    assert deps["some-unlisted-lib"] == "*"                    # installable, never a wrong pin
    assert "@team/widgets" not in deps                         # unknown scope -> probably an alias
    assert "AppError" not in deps                              # not a legal npm name -> a code bug


def test_monorepo_imports_go_to_the_nearest_manifest() -> None:
    files = {
        "backend/package.json": json.dumps({"dependencies": {}}),
        "backend/src/server.js": "const pg = require('pg');\n",
        "frontend/package.json": json.dumps({"dependencies": {}}),
        "frontend/src/Chart.jsx": "import { LineChart } from 'recharts';\n",
    }
    changed = reconcile_package_dependencies(files)
    assert "pg" in _deps(changed, "backend/package.json")["dependencies"]
    assert "pg" not in _deps(changed, "frontend/package.json")["dependencies"]
    assert "recharts" in _deps(changed, "frontend/package.json")["dependencies"]


def test_manifest_formatting_and_key_order_are_preserved() -> None:
    original = (
        '{\n'
        '    "name": "api",\n'
        '    "version": "1.0.0",\n'
        '    "scripts": {"start": "node src/server.js"},\n'
        '    "dependencies": {"express": "^4.19.2"}\n'
        '}\n'
    )
    changed = reconcile_package_dependencies({
        "package.json": original,
        "src/server.js": "const cors = require('cors');\nconst express = require('express');\n",
    })
    out = changed["package.json"]
    assert out.startswith('{\n    "name"') and out.endswith("}\n")     # 4-space indent + trailing NL
    assert list(json.loads(out)) == ["name", "version", "scripts", "dependencies"]
    assert list(json.loads(out)["dependencies"]) == ["cors", "express"]  # alphabetical stays so


def test_multiline_and_commented_out_imports_are_read_correctly() -> None:
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "src/store.js": (
            "// const nope = require('helmet');\n"             # commented out -> not a dependency
            "/* import x from 'morgan'; */\n"
            "import {\n  useSelector,\n  useDispatch,\n} from 'react-redux';\n"
            "import 'recharts';\n"                             # bare side-effect import
        ),
    }
    deps = _deps(reconcile_package_dependencies(files))["dependencies"]
    assert set(deps) == {"react-redux", "recharts"}


# --- unresolvable local imports (report-only) --------------------------------------------------

BROKEN_IMPORTS = {
    "src/app.js": (
        "const AppError = require('./utils/AppError');\n"      # nobody generated it
        "const db = require('./config/db');\n"                 # generated as src/db.js instead
        "const logger = require('./utils/logger');\n"          # resolves
        "const routes = require('./routes');\n"                # resolves via routes/index.js
        "// const ghost = require('./ghost');\n"               # commented out -> not a finding
    ),
    "src/utils/logger.js": "module.exports = console;\n",
    "src/routes/index.js": "module.exports = {};\n",
    "src/db.js": "module.exports = {};\n",
}


def test_unresolvable_relative_imports_are_reported_and_resolvable_ones_are_not() -> None:
    found = find_unresolved_imports(BROKEN_IMPORTS)
    assert [(f.importer, f.specifier) for f in found] == [
        ("src/app.js", "./config/db"),
        ("src/app.js", "./utils/AppError"),
    ]
    by_spec = {f.specifier: f for f in found}
    assert by_spec["./utils/AppError"].target == "src/utils/AppError"
    # The "same module, two conventions" case carries the existing file as evidence — but is NOT
    # rewritten: creating a shim vs renaming the importer is the debugging agent's call.
    assert by_spec["./config/db"].candidates == ("src/db.js",)
    assert "src/db.js" in by_spec["./config/db"].as_note()


def test_unresolved_report_changes_nothing_and_is_deterministic() -> None:
    assert reconcile_wiring(BROKEN_IMPORTS) == {}              # report-only: no file is rewritten
    assert find_unresolved_imports(BROKEN_IMPORTS) == find_unresolved_imports(BROKEN_IMPORTS)


def test_index_extension_and_ts_style_resolution_are_understood() -> None:
    files = {
        "src/main.ts": (
            "import { Button } from './components/Button';\n"      # -> Button.tsx
            "import { api } from './lib/api.js';\n"                # NodeNext: .js on disk is .ts
            "import { store } from './store';\n"                   # -> store/index.ts
        ),
        "src/components/Button.tsx": "export const Button = () => null;\n",
        "src/lib/api.ts": "export const api = {};\n",
        "src/store/index.ts": "export const store = {};\n",
        # Above the root of the file set: nothing outside it is visible here, so it is not judged.
        "index.js": "require('../shared/thing');\n",
    }
    assert find_unresolved_imports(files) == []


def test_package_imports_are_not_treated_as_unresolved_modules() -> None:
    files = {"src/a.js": "const express = require('express');\nimport React from 'react';\n"}
    assert find_unresolved_imports(files) == []                # bare specifiers are the fixer's job
