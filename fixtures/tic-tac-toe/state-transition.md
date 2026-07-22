# State Transition — Tic-Tac-Toe

This document describes the application's state machine. The game is a finite
state machine (FSM) with a small, well-defined set of states and transitions.

---

## States

| State | Description |
|-------|-------------|
| **Idle** | Initial state immediately after load. Board is empty. Effectively identical to the start of **Playing**; may be treated as the entry point into Playing with X to move. |
| **Playing** | The game is in progress. Cells may be filled by the current player. This is the main interactive state. |
| **Winner** | A terminal state. One player has completed a winning combination. No moves are accepted. |
| **Draw** | A terminal state. The board is full and no winner exists. No moves are accepted. |

---

## Transition Diagram

```
        (load)
          │
          ▼
       ┌──────┐
       │ Idle │
       └──────┘
          │  first move / game begins
          ▼
      ┌─────────┐   valid move (no win, board not full)
      │ Playing │◄─────────────────────────────┐
      └─────────┘                               │
        │     │                                 │
        │     │ valid move completes a line      │
        │     ▼                                 │
        │  ┌────────┐                           │
        │  │ Winner │──── Reset ────────────────┤
        │  └────────┘                           │
        │                                       │
        │ valid move fills last empty cell,     │
        │ no winning line                       │
        ▼                                       │
     ┌──────┐                                   │
     │ Draw │──────── Reset ────────────────────┘
     └──────┘
```

The canonical happy-path cycle:

```
Idle
  ↓  (game begins)
Playing
  ↓  (three in a row)
Winner
  ↓  (Reset)
Playing
  ↓  (board fills, no winner)
Draw
  ↓  (Reset)
Playing
```

---

## Transition Table

| From | Event | Guard / Condition | To |
|------|-------|-------------------|-----|
| Idle | app load | — | Idle (empty board, X to move) |
| Idle / Playing | place marker | target cell empty **and** move does not complete a line **and** board not full | Playing (turn switches) |
| Playing | place marker | move completes a winning combination | Winner |
| Playing | place marker | board becomes full **and** no winning combination | Draw |
| Winner | Reset | — | Playing (empty board, X to move) |
| Draw | Reset | — | Playing (empty board, X to move) |
| Playing | Reset | — | Playing (empty board, X to move) |
| Winner / Draw | place marker | (any) | ignored — no transition |
| Playing | place marker | target cell occupied | ignored — no transition |

---

## Notes

- **Current player** is derived from move count within the Playing state and is
  not a separate FSM state.
- Terminal states (**Winner**, **Draw**) can only be exited via **Reset**.
- The invalid transitions (occupied cell, moves after game over) are enforced by
  [validation-rules.md](validation-rules.md).
