# Backend Project Structure — Tic-Tac-Toe

This project has **no backend game logic**. The game is fully client-side. A
minimal backend is included **only** to serve a health-check endpoint (useful for
deployment liveness probes and static-host wrappers).

There are intentionally **no controllers, no services, no models, and no database**.

---

## Directory Layout

```
backend/
    package.json
    server.js
    routes/
        health.js
```

---

## File Responsibilities

| File | Responsibility |
|------|----------------|
| `package.json` | Declares backend metadata, the `start` script, and a single runtime dependency (a minimal HTTP framework such as Express). No build step. |
| `server.js` | The entry point. Creates the HTTP server/app, mounts the `health` route, and listens on a fixed default port (e.g. `3001`). Contains no game logic and reads no environment variables. |
| `routes/health.js` | Defines a single `GET /health` route that returns a simple JSON liveness response, e.g. `{ "status": "ok" }` with HTTP 200. |

---

## Explicit Non-Goals

Per the project constraints in [extracted-requirements.md](extracted-requirements.md),
the backend deliberately excludes:

- ❌ **Controllers** — no request-handling layer beyond the single health route.
- ❌ **Services** — no business/domain logic.
- ❌ **Models** — no data entities.
- ❌ **Database** — nothing is persisted.
- ❌ **Game logic / APIs** — all rules run in the frontend.
- ❌ **Authentication** — the health endpoint is public.
- ❌ **Environment variables** — the port is a hard-coded default.

---

## Notes

- The backend is optional at runtime: the frontend functions completely without it.
- The health endpoint exists purely so container/platform orchestrators can verify
  the deployment is alive.
- Keep `server.js` under a few dozen lines; complexity here is a smell.
