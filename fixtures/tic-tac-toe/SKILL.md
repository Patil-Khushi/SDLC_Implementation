# SKILL.md — Coding Guidelines for the Implementation Agent

These are the coding standards the implementation agent must follow when building
the Tic-Tac-Toe frontend from these design artifacts. Treat this as authoritative.

---

## 1. React Best Practices

- Use **function components** exclusively — no class components.
- Keep components **pure**: given the same props/state, render the same output.
- Derive values during render instead of duplicating them in state (e.g. compute
  the current player and winner from the board, don't store them separately).
- Lift shared state to the nearest common ancestor; pass data down via props and
  behavior up via callbacks.
- Keep components **small and single-purpose**. If a component does two things,
  split it.
- Never mutate state directly — always produce a new array/object.
- Avoid premature optimization; only reach for `React.memo`/`useMemo`/`useCallback`
  when a real performance need is demonstrated.

## 2. Component Naming

- Components use **PascalCase**: `Board`, `Cell`, `Status`, `ResetButton`.
- One component per file; the filename matches the component name (`Board.jsx`).
- Props use **camelCase**: `onCellClick`, `isDisabled`, `value`.
- Boolean props read as adjectives/questions: `isDisabled`, `isWinningCell`.
- Event-handler props are prefixed with `on`; handlers are prefixed with `handle`
  (`onCellClick` prop → `handleCellClick` implementation).

## 3. Folder Organization

Follow the layout in [frontend-project-structure.md](frontend-project-structure.md):

- `components/` — presentational + container components.
- `hooks/` — custom hooks (game logic lives in `useGame`).
- `utils/` — pure helper functions (winner detection).
- `styles/` — CSS files.
- `assets/` — static assets.

Group by **type** (components/hooks/utils), not by feature, given the app's small size.

## 4. Hooks

- Encapsulate all game state and rules in a single custom hook: **`useGame()`**.
- `useGame` returns a clear API, e.g. `{ board, status, currentPlayer, winner, isDraw, playMove, reset }`.
- Custom hook names always start with `use`.
- Respect the Rules of Hooks: call hooks only at the top level, never conditionally.
- Keep side effects out of render; use `useEffect` only when genuinely needed
  (this app likely needs none for core logic).

## 5. State Management

- **Local React state only** — no Redux, Zustand, or Context needed for this scope.
- The single source of truth is the **board array** (length 9) plus a small status.
- Derive `currentPlayer`, `winner`, and `isDraw` from the board rather than storing them.
- No persistence: state resets on reload (per constraints in
  [extracted-requirements.md](extracted-requirements.md)).

## 6. CSS Conventions

- Use plain CSS (or CSS Modules) in `styles/`. No CSS-in-JS libraries.
- Drive all colors, spacing, radii, and typography from
  [design-tokens.json](design-tokens.json) — mirror them as CSS custom properties
  (`:root { --color-marker-x: #38bdf8; ... }`).
- Use **BEM-like** class names: `.board`, `.board__cell`, `.board__cell--filled`.
- Use `rem` for sizing/spacing and CSS Grid for the board layout.
- Make the layout responsive with fluid units (`min()`, `clamp()`) and media queries
  at the breakpoints defined in the tokens.

## 7. Accessibility

- Cells must be real `<button>` elements — focusable and keyboard-activatable.
- Provide an `aria-label` per cell describing its position and content
  (e.g. "Row 1, Column 2, empty").
- Announce status changes via an ARIA live region (`aria-live="polite"`).
- Maintain a visible focus ring (use the `shadow.focus` token).
- Ensure color contrast meets WCAG AA; never rely on color alone to convey the winner.

## 8. Reusable Components

- Build small, composable components: `Cell` is reused 9 times by `Board`.
- Keep `Cell` presentational (dumb) — it receives `value` and `onClick`, holds no logic.
- Extract a generic `Button` only if more than one button type appears; otherwise
  `ResetButton` is sufficient.

## 9. No Inline Styles

- **Do not** use the `style={{ ... }}` prop for static styling.
- All styling goes through CSS classes backed by design tokens.
- The only acceptable inline style is a genuinely dynamic value that cannot be
  expressed as a class (avoid if possible).

## 10. File Naming

- Component files: **PascalCase** with `.jsx` (`ResetButton.jsx`).
- Hook files: **camelCase** with `.js` (`useGame.js`).
- Utility files: **camelCase** with `.js` (`winner.js`).
- Style files: **kebab-case** with `.css` (`app.css`).
- One default export per component/hook file.

## 11. Formatting

- Use **Prettier** defaults: 2-space indentation, single quotes, semicolons,
  trailing commas where valid, 80–100 char print width.
- Use **ESLint** with the React and React-Hooks recommended rule sets.
- Prefer `const`; use `let` only when reassignment is required; never `var`.
- Prefer arrow functions for callbacks and named function declarations for components.
- Keep imports ordered: external packages first, then internal modules, then styles.

---

## Definition of Done (coding)

- Lint and format pass with no warnings.
- No inline styles; all tokens sourced from `design-tokens.json`.
- Component/file naming matches sections 2 and 10.
- All validation rules in [validation-rules.md](validation-rules.md) are enforced.
- Keyboard and screen-reader accessibility verified.
