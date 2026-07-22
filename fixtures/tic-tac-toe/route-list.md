# Route List — Tic-Tac-Toe

The application is a small single-page app. Routing is minimal; only the game
route is required. An optional informational route may be included.

| Route | Name | Required | Purpose |
|-------|------|----------|---------|
| `/` | Game | ✅ Required | The main and only gameplay screen. Renders the header, the 3×3 board, the turn/outcome status, and the Reset button. This is the app's landing page. |
| `/about` | About | ⬜ Optional | A static informational page describing the game, the rules of Tic-Tac-Toe, and credits. Contains no gameplay. May be omitted entirely if not desired. |

## Notes

- No authentication guards any route — all routes are public.
- No dynamic or parameterized routes exist (no `/game/:id`, etc.).
- Deep-linking is not required; refreshing `/` starts a fresh game.
- If `/about` is implemented, provide a link to and from `/` for navigation.
- A client-side router (e.g. React Router) is only necessary if `/about` is
  included; for a single route, the app may render the game directly without a router.

## Related Artifacts

- The screens rendered per route map to the components in [frontend-project-structure.md](frontend-project-structure.md).
