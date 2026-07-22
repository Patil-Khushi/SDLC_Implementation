# Extracted Requirements — Tic-Tac-Toe

## 1. Overview

A simple, client-side, two-player Tic-Tac-Toe game played on a 3×3 grid. The
application runs entirely in the browser with no backend logic, no persistence,
and no authentication. Two humans share the same device and alternate turns.

---

## 2. Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-01 | The game shall provide a 3×3 board consisting of 9 cells. |
| FR-02 | The game shall support exactly two players, using markers **X** and **O**. |
| FR-03 | Player **X** shall always make the first move of a new game. |
| FR-04 | Players shall alternate turns after every valid move. |
| FR-05 | A move shall place the current player's marker into an empty cell. |
| FR-06 | The game shall reject any attempt to play on an already-occupied cell. |
| FR-07 | The game shall reject any move after the game has ended (win or draw). |
| FR-08 | The game shall detect a win across any of the 3 rows (horizontal). |
| FR-09 | The game shall detect a win across any of the 3 columns (vertical). |
| FR-10 | The game shall detect a win across either of the 2 diagonals. |
| FR-11 | The game shall declare a draw when all 9 cells are filled with no winner. |
| FR-12 | The game shall continuously display whose turn it is while playing. |
| FR-13 | The game shall display the outcome (winner or draw) when the game ends. |
| FR-14 | The game shall provide a **Reset** control that clears the board and starts a new game with Player X to move. |
| FR-15 | The winning line shall be visually highlighted when a win occurs (optional enhancement). |

---

## 3. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-01 | **Responsive UI** — the board shall render correctly on mobile, tablet, and desktop viewports. |
| NFR-02 | **Performance** — a move and its result shall render in under 100 ms. |
| NFR-03 | **Accessibility** — the board shall be keyboard-navigable and expose ARIA labels for cells and status. |
| NFR-04 | **Maintainability** — UI, game logic, and styling shall be separated into distinct modules. |
| NFR-05 | **Portability** — the app shall run on the latest two versions of Chrome, Firefox, Edge, and Safari. |
| NFR-06 | **No external runtime dependencies** beyond the React runtime and build tooling. |
| NFR-07 | **Stateless** — the app shall hold no state beyond the current browser session (in-memory only). |

---

## 4. Assumptions

- A-01: Both players use the same device and browser (hot-seat / pass-and-play).
- A-02: No network connection is required after the initial page load.
- A-03: No user accounts, profiles, or scores are stored between sessions.
- A-04: The build/deploy target is a static host (any static file server).
- A-05: Refreshing the page is an acceptable alternative to the Reset button and starts a fresh game.
- A-06: There is no AI/computer opponent; both players are human.

---

## 5. Constraints

- C-01: **No authentication** of any kind.
- C-02: **No database** and **no persistence** (no localStorage, no cookies).
- C-03: **No backend business logic** — game rules run entirely in the client.
- C-04: **No APIs** are consumed or exposed for gameplay.
- C-05: **No environment variables** are required to run the app.
- C-06: A minimal backend exists **only** for a health-check endpoint (see [backend-project-structure.md](backend-project-structure.md)); it contains no game logic.
- C-07: Frontend built with **React** (see [SKILL.md](SKILL.md) for conventions).

---

## 6. Acceptance Criteria

- AC-01: Given a new game, when the app loads, then an empty 3×3 board is shown and the status reads "X's turn".
- AC-02: Given it is X's turn, when X clicks an empty cell, then an X appears there and the status changes to "O's turn".
- AC-03: Given a cell already contains a marker, when a player clicks it, then nothing changes.
- AC-04: Given three matching markers in any row, column, or diagonal, when the third is placed, then the status reads "X wins" or "O wins" and no further moves are accepted.
- AC-05: Given all 9 cells are filled with no winning line, then the status reads "Draw" and no further moves are accepted.
- AC-06: Given any game state, when the Reset button is clicked, then the board clears and the status returns to "X's turn".
- AC-07: Given a narrow (mobile) viewport, when the app renders, then the board remains square, fully visible, and tappable without horizontal scrolling.
- AC-08: Given a keyboard-only user, when they Tab to a cell and press Enter/Space, then the move is placed exactly as a click would.
