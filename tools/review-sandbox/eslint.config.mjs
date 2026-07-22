// Flat ESLint config (ESLint v9) baked into the review-sandbox image. The Code Review agent runs:
//   eslint --no-config-lookup --config /opt/eslint/eslint.config.mjs -f json .
// against the CLONED repo, deliberately ignoring that repo's own eslint config. It lints the
// JS/TS/JSX/TSX sources the agent scans; typescript-eslint provides the TS parser so .ts/.tsx
// files don't fail with a parser error.
import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default [
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{js,jsx,mjs,cjs,ts,tsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
  },
  // Never lint dependencies or build output in the cloned repo.
  { ignores: ["**/node_modules/**", "**/dist/**", "**/build/**", "**/.venv/**"] },
];
