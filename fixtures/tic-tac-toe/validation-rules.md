# Validation Rules — Tic-Tac-Toe

All gameplay rules the implementation must enforce. These run entirely in the
client (see [SKILL.md](SKILL.md) and [frontend-project-structure.md](frontend-project-structure.md)).
Terms are defined in [glossary.md](glossary.md).

---

## VR-01 — Cannot overwrite occupied cells

A move targeting a cell that already holds a marker must be **rejected**. The board
and turn remain unchanged.

- **Given** a cell contains `X` or `O`
- **When** any player attempts to play that cell
- **Then** the move is ignored; no state changes.

## VR-02 — Only X or O allowed

The only valid markers are `X` and `O`. No other value may be written to a cell.
A cell is otherwise empty (`null` / empty string).

## VR-03 — Player X starts first

Every new game (initial load or after Reset) begins with **Player X** to move.

## VR-04 — Alternate turns

Turns strictly alternate. After a valid X move it becomes O's turn, and vice versa.
The current player is derived from the count of placed markers:

- even count (0, 2, 4, 6, 8) → **X** to move
- odd count (1, 3, 5, 7) → **O** to move

No player may take two consecutive turns.

## VR-05 — No moves after a winner

Once a winner has been detected, the game is over. Any subsequent move attempt
must be **rejected** until Reset.

## VR-06 — No moves after a draw

Once a draw has been detected (board full, no winner), any move attempt must be
**rejected** until Reset.

## VR-07 — Reset clears the board

The Reset action must:

- empty all 9 cells,
- clear any winner/draw outcome,
- return the current player to **X**,
- return the game to the **Playing** state.

Reset is valid from any state (Playing, Winner, Draw).

## VR-08 — Winner detection rules

A win occurs when a single marker occupies all three cells of any **winning
combination**. The 8 combinations (by cell index) are:

| Type | Combinations |
|------|--------------|
| Rows | `[0,1,2]`, `[3,4,5]`, `[6,7,8]` |
| Columns | `[0,3,6]`, `[1,4,7]`, `[2,5,8]` |
| Diagonals | `[0,4,8]`, `[2,4,6]` |

- Winner detection runs **after each valid move**.
- The **first** completed combination determines the winner.
- Only the player who just moved can become the winner on that move.

## VR-09 — Draw detection rules

A draw is declared when **all 9 cells are filled** and **no winning combination
exists**. Draw detection runs after winner detection (a full board that also
completes a line is a **win**, not a draw).

## VR-10 — Move validity summary

A move is **valid** only if **all** hold:

1. The game is in the **Playing** state (not Winner, not Draw). — VR-05, VR-06
2. The target cell is **empty**. — VR-01
3. The marker placed is that of the **current player**. — VR-02, VR-04

If any condition fails, the move is ignored with no state change.

---

## Validation Order (per move)

```
1. Is the game over (Winner/Draw)?      → yes: reject (VR-05/VR-06)
2. Is the target cell occupied?         → yes: reject (VR-01)
3. Place current player's marker.       → (VR-02, VR-04)
4. Check for a winner.                  → yes: enter Winner state (VR-08)
5. Else, is the board full?             → yes: enter Draw state (VR-09)
6. Else, switch turn; stay Playing.     → (VR-04)
```
