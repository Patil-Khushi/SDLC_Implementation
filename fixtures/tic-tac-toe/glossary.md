# Glossary — Tic-Tac-Toe

Definitions of the core domain terms used across all design artifacts. These
terms should be used consistently in code, comments, and UI copy.

| Term | Definition |
|------|------------|
| **Board** | The 3×3 grid that holds all playable positions. Modeled as an ordered collection of 9 cells (indices 0–8, left-to-right, top-to-bottom). |
| **Cell** | A single position on the board. A cell is either **empty** or holds exactly one marker (`X` or `O`). Cells are addressed by index 0–8. |
| **Marker** | The symbol a player places into a cell — either `X` or `O`. |
| **Turn** | A single opportunity for the current player to place one marker into one empty cell. After a valid move, the turn passes to the other player. |
| **Player** | One of the two participants. Player **X** always moves first; Player **O** moves second. There is no computer opponent. |
| **Current Player** | The player whose turn it is to move. Derived from the number of markers already placed (even count → X, odd count → O). |
| **Winner** | The player who first completes a **winning combination**. Once a winner exists, the game is over and no further moves are accepted. |
| **Draw** | The terminal outcome where all 9 cells are filled and no winning combination exists. Also called a "tie" or "cat's game". |
| **Reset** | The action that clears the board, discards the current outcome, and returns the game to the starting state with Player X to move. |
| **Game State** | The complete in-memory representation of the game at a point in time: the contents of all cells, whose turn it is, and the current status (playing, won, or draw). |
| **Winning Combination** | Any of the 8 index triples that constitute a win: three rows `[0,1,2] [3,4,5] [6,7,8]`, three columns `[0,3,6] [1,4,7] [2,5,8]`, and two diagonals `[0,4,8] [2,4,6]`. A win occurs when all three cells of a combination hold the same marker. |
| **Status** | Human-readable description of the current game state shown to players (e.g. "X's turn", "O wins", "Draw"). |
| **Move** | A single, validated placement of the current player's marker into an empty cell. |

## Related Artifacts

- Winning combinations are enforced by the rules in [validation-rules.md](validation-rules.md).
- Game State transitions are described in [state-transition.md](state-transition.md).
