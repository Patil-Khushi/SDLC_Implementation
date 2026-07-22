# User Features — Tic-Tac-Toe

User stories describing the features from the player's perspective. Each story
follows the format: *As a … I want … so that …*, with acceptance criteria.

---

## US-01 — Start a game

**As a** player
**I want** the game to open with a fresh, empty board
**so that** I can begin playing immediately without any setup.

**Acceptance Criteria**
- On load, all 9 cells are empty.
- The status indicates Player X moves first.
- No configuration, login, or menu is required.

---

## US-02 — Place a marker

**As a** player
**I want** to click (or tap) an empty cell on my turn
**so that** I can place my marker and make my move.

**Acceptance Criteria**
- Clicking an empty cell during my turn places my marker there.
- Clicking an occupied cell has no effect.
- After my valid move, the turn passes to the other player.

---

## US-03 — See the current turn

**As a** player
**I want** a clear indicator of whose turn it is
**so that** both players know who should move next.

**Acceptance Criteria**
- The status text reads "X's turn" or "O's turn" while the game is in progress.
- The indicator updates immediately after every valid move.

---

## US-04 — Detect a winner

**As a** player
**I want** the game to recognize when someone has three in a row
**so that** the game ends and the winner is announced.

**Acceptance Criteria**
- When a player completes any row, column, or diagonal, the status announces that player as the winner.
- No further moves are accepted after a winner is declared.
- (Optional) The winning line is visually highlighted.

---

## US-05 — Detect a draw

**As a** player
**I want** the game to recognize when the board is full with no winner
**so that** we know the game is a tie.

**Acceptance Criteria**
- When all 9 cells are filled and no winning combination exists, the status announces a draw.
- No further moves are accepted after a draw.

---

## US-06 — Restart the game

**As a** player
**I want** a reset button
**so that** we can play again without reloading the page.

**Acceptance Criteria**
- A visible Reset control is always available.
- Clicking Reset clears the board and returns the game to X's turn.
- Reset works from any state (mid-game, won, or draw).

---

## US-07 — Play on any device (responsive)

**As a** player on a phone, tablet, or desktop
**I want** the board to fit and remain usable on my screen
**so that** I can play comfortably anywhere.

**Acceptance Criteria**
- The board stays square and fully visible with no horizontal scroll.
- Cells are large enough to tap on touch devices.

---

## US-08 — Play using the keyboard (accessibility)

**As a** keyboard or assistive-technology user
**I want** to navigate and play using the keyboard
**so that** the game is accessible to me.

**Acceptance Criteria**
- Cells are focusable and activatable with Enter/Space.
- The status is announced to screen readers via an ARIA live region.
