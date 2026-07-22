# Frontend Project Structure — Tic-Tac-Toe

The desired directory layout for the React frontend, built with Vite. Every file's
responsibility is described below so the implementation agent can scaffold it directly.

---

## Directory Layout

```
frontend/
    package.json
    vite.config.js
    index.html
    src/
        App.jsx
        main.jsx
        assets/
        components/
            Board.jsx
            Cell.jsx
            Status.jsx
            ResetButton.jsx
        hooks/
            useGame.js
        utils/
            winner.js
        styles/
            app.css
```

---

## File Responsibilities

### Root

| File | Responsibility |
|------|----------------|
| `package.json` | Declares project metadata, scripts (`dev`, `build`, `preview`, `lint`), and dependencies (React, React-DOM) plus dev dependencies (Vite, ESLint, Prettier). |
| `vite.config.js` | Vite build/dev-server configuration with the React plugin. Minimal — no proxies or env handling needed. |
| `index.html` | The HTML entry point. Contains the `<div id="root">` mount node and a `<script type="module" src="/src/main.jsx">`. |

### `src/`

| File | Responsibility |
|------|----------------|
| `main.jsx` | Application bootstrap. Creates the React root and renders `<App />` into `#root`. Imports global styles. |
| `App.jsx` | Top-level component. Composes the layout: header/title, `<Status />`, `<Board />`, and `<ResetButton />`. Owns the `useGame` hook and wires its API into child components. |

### `src/assets/`

| Path | Responsibility |
|------|----------------|
| `assets/` | Static assets (favicon, images, fonts if bundled). May be empty for the base game. |

### `src/components/`

| File | Responsibility |
|------|----------------|
| `Board.jsx` | Renders the 3×3 grid by mapping the board array to 9 `<Cell />` components. Receives `board` and `onCellClick` as props. Presentational container — holds no game rules. |
| `Cell.jsx` | Renders a single cell as a `<button>` showing its marker (`X`, `O`, or empty). Receives `value`, `onClick`, `disabled`, and optional `isWinningCell`. Purely presentational. |
| `Status.jsx` | Displays the current game status ("X's turn", "O wins", "Draw"). Receives `status` text. Uses an ARIA live region for accessibility. |
| `ResetButton.jsx` | Renders the Reset control. Receives an `onReset` callback and triggers a new game. |

### `src/hooks/`

| File | Responsibility |
|------|----------------|
| `useGame.js` | The single source of game state and logic. Manages the board array, computes current player, winner, and draw status, and exposes `{ board, status, currentPlayer, winner, isDraw, playMove, reset }`. Enforces all rules from [validation-rules.md](validation-rules.md). |

### `src/utils/`

| File | Responsibility |
|------|----------------|
| `winner.js` | Pure helper functions for outcome detection. Contains the 8 winning combinations and `calculateWinner(board)` returning the winning marker (and optionally the winning line) or `null`. No React dependencies. |

### `src/styles/`

| File | Responsibility |
|------|----------------|
| `app.css` | Global styles and component classes. Defines CSS custom properties mirroring [design-tokens.json](design-tokens.json), the board grid, cell styling, status, button styles, focus rings, and responsive media queries. |

---

## Conventions

- File and component naming follows [SKILL.md](SKILL.md) sections 2 and 10.
- Game logic lives only in `hooks/useGame.js` and `utils/winner.js`; components stay presentational.
- All styling flows through `styles/app.css` — no inline styles.
