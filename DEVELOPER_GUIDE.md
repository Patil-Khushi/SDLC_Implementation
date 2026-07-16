# Implementation Service — Developer Guide

A simple, complete walkthrough of how this project is built and how to work in
it. If you are new here, **read this top to bottom once**, then use the "How to
add a new agent" section as your day-to-day recipe.

---

## 1. What this service does (in one minute)

This service takes a **Design Package** (a description of what to build) and turns
it into an **Implementation Package** (real source code, tests, docs, and a
security report). It does this by running a series of **AI agents** one after
another, each doing one job.

The order of agents (the "pipeline") is:

```
Design Package
   -> Code Generation   (writes the code)
   -> Code Review        (reviews the code)
   -> Refactoring        (cleans up the code)
   -> Debugging          (fixes problems)
   -> Unit Test          (writes tests)
   -> Documentation      (writes docs)
   -> Security           (scans for issues)
-> Implementation Package
```

Right now only the first agent (Code Generation) is built. The rest are
scaffolded and waiting to be filled in — the whole point of this guide is so
everyone adds them the **same way**.

---

## 2. The big picture (how the pieces fit)

There are four layers. Each has ONE job. Keep them separate — do not mix them.

```
   ┌─────────────────────────────────────────────────────────┐
   │  FastAPI (the web layer)                                 │
   │  Receives HTTP requests, checks the input, sends a reply │
   │  DOES NOT contain any agent logic                        │
   └───────────────────────────┬─────────────────────────────┘
                               │ calls
                               ▼
   ┌─────────────────────────────────────────────────────────┐
   │  LangGraph (the workflow engine)                         │
   │  Runs the agents in the right order, carries the "state" │
   │  DOES NOT write code or call the AI itself               │
   └───────────────────────────┬─────────────────────────────┘
                               │ runs each
                               ▼
   ┌─────────────────────────────────────────────────────────┐
   │  Agents (the workers)                                    │
   │  Each agent does ONE task (generate, review, test, ...)  │
   │  DOES NOT call the AI SDK directly                       │
   └───────────────────────────┬─────────────────────────────┘
                               │ calls
                               ▼
   ┌─────────────────────────────────────────────────────────┐
   │  LLM Gateway (the single door to the AI)                 │
   │  The ONLY place that talks to Claude                     │
   └─────────────────────────────────────────────────────────┘
```

**Why this matters:** if every agent talked to the AI directly, or the web layer
had agent logic inside it, the project would become a tangle. These boundaries
keep it clean and easy to grow.

---

## 3. The starting point

When the app boots, everything begins in **`app/main.py`**. It:

1. Loads settings (from `.env`).
2. Creates the FastAPI app.
3. Connects the API routes.

From there, a real request flows like this:

```
1. Someone calls  POST /implementation/start  with a design package
2. app/api/routes.py       receives it, builds the starting "state"
3. app/graph/graph.py      runs the workflow (the compiled LangGraph)
4. app/graph/nodes.py      runs the code_generator node
5. app/agents/code_generator.py   does the work
6. app/services/llm_gateway.py     calls Claude and returns the text
7. the result flows back up and routes.py returns the JSON response
```

That is the entire journey. Every new agent plugs into steps 4–5.

---

## 4. Folder structure — what every file is for

```
services/implementation/
│
├── requirements.txt        # List of Python packages the project needs
├── .env.example            # Template for secrets/config (copy to .env)
├── .env                    # YOUR real secrets (never committed to git)
│
└── app/
    │
    ├── main.py             # START HERE. Boots FastAPI, connects routes.
    │
    ├── config/
    │   └── settings.py     # Reads config from .env (API key, model, etc.)
    │
    ├── api/                # THE WEB LAYER (input/output only, no logic)
    │   ├── routes.py           # The HTTP endpoints (e.g. /start)
    │   ├── request_models.py   # Shape of the data coming IN
    │   └── response_models.py  # Shape of the data going OUT
    │
    ├── graph/              # THE WORKFLOW ENGINE (order of agents)
    │   ├── state.py            # WorkflowState: the shared "clipboard"
    │   ├── nodes.py            # Wraps each agent as a graph "node"
    │   ├── graph.py            # Connects nodes in order, compiles graph
    │   └── router.py           # (later) rules for branching/looping
    │
    ├── agents/             # THE WORKERS (one file per agent)
    │   ├── base.py             # BaseAgent: shared parent for all agents
    │   ├── code_generator.py   # DONE - example agent to copy
    │   ├── code_review.py      # empty - to be built
    │   ├── refactoring.py      # empty - to be built
    │   ├── debugging.py        # empty - to be built
    │   ├── unit_test.py        # empty - to be built
    │   ├── documentation.py    # empty - to be built
    │   └── security.py         # empty - to be built
    │
    ├── services/           # SHARED HELPERS
    │   ├── llm_gateway.py      # The ONLY place that talks to Claude
    │   ├── artifact_service.py # (later) read/save design & code files
    │   ├── workspace_service.py# (later) manage temp project folders
    │   ├── parser.py           # (later) parse existing source code
    │   └── retriever.py        # (later) give the AI extra context
    │
    ├── integrations/       # WRAPPERS FOR OUTSIDE TOOLS (later)
    │   ├── github.py           # talk to GitHub
    │   ├── semgrep.py          # run security scans
    │   ├── sonarqube.py        # code quality
    │   ├── pytest_runner.py    # run tests
    │   └── docker.py           # containers
    │
    ├── prompts/            # THE AI INSTRUCTIONS (one .md per agent)
    │   └── code_generation.md  # System prompt for the code generator
    │
    ├── templates/          # (later) starter templates (React, FastAPI, ...)
    ├── workspace/          # (later) temporary files made during a run
    ├── models/             # (later) shared data models
    ├── utils/              # (later) small helper functions
    └── tests/              # Automated tests
        ├── test_health.py      # checks the /health endpoint
        └── test_workflow.py    # runs the workflow with a fake AI
```

---

## 5. The four ideas you must understand

Everything else is detail. Understand these four and you can work anywhere in
the project.

### A. WorkflowState — the shared clipboard  (`app/graph/state.py`)

This is a dictionary that travels through the whole pipeline. Each agent reads
what it needs and writes its own result. Think of it as a clipboard passed from
worker to worker.

```python
class WorkflowState(TypedDict, total=False):
    project_id: str
    design_package: str      # input
    generated_code: str      # code_generator writes this
    review_report: str       # code_review writes this
    refactored_code: str     # refactoring writes this
    unit_tests: str          # unit_test writes this
    documentation: str       # documentation writes this
    security_report: str     # security writes this
    workflow_status: str     # where we are in the pipeline
```

**Golden rule:** an agent updates ONLY the fields it owns. The code reviewer
reads `generated_code` and writes `review_report` — it never touches
`generated_code`.

### B. BaseAgent — the shared parent  (`app/agents/base.py`)

Every agent inherits from `BaseAgent`. It gives each agent two things for free:

- `self.llm` — access to the LLM Gateway (to call the AI).
- `self._load_prompt("name")` — loads the matching file from `app/prompts/`.

And it requires every agent to have one method:

```python
def execute(self, state) -> state
```

This is the contract: **give me the state, I do my job, I hand the state back.**

### C. LLM Gateway — the single door to the AI  (`app/services/llm_gateway.py`)

This is the ONLY file that imports the `anthropic` (Claude) SDK. Every agent
calls the AI through it:

```python
answer = self.llm.complete(prompt="...", system="...")
```

**Why one door?** So that retries, logging, cost tracking, and switching AI
models happen in ONE place. Never `import anthropic` inside an agent.

### D. The Graph — the order of work  (`app/graph/graph.py`)

LangGraph is a "state machine". You register each agent as a **node**, then draw
**edges** (arrows) between them to set the order. `START` and `END` are the
entry and exit.

```python
graph.add_node("code_generator", nodes.code_generator_node)
graph.add_edge(START, "code_generator")   # begin here
graph.add_edge("code_generator", END)     # then finish
```

Adding an agent = add its node + redraw the arrows.

---

## 6. How to add a new agent (the recipe)

This is the part you will use most. Let's add the **Code Review agent** as an
example. Do these 4 steps in order.

### Step 1 — Write the prompt

Create `app/prompts/code_review.md`. This is plain English telling the AI how to
behave:

```markdown
You are a senior code reviewer in an automated SDLC pipeline.
Review the given source code for bugs, bad practices, and unclear naming.
Return a clear list of findings, each with a severity (high/medium/low).
```

### Step 2 — Fill in the agent

Open `app/agents/code_review.py` (it's an empty file) and write:

```python
from app.agents.base import BaseAgent
from app.graph.state import WorkflowState


class CodeReviewAgent(BaseAgent):
    name = "code_review"

    def execute(self, state: WorkflowState) -> WorkflowState:
        report = self.llm.complete(
            prompt=f"Review this code:\n\n{state['generated_code']}",
            system=self._load_prompt("code_review"),
        )
        state["review_report"] = report      # writes ONLY its own field
        state["workflow_status"] = "code_reviewed"
        return state
```

Notice: it **reads** `generated_code` (made by the previous agent) and **writes**
`review_report`. That is how agents pass work down the line.

### Step 3 — Add a node

Open `app/graph/nodes.py` and add:

```python
from app.agents.code_review import CodeReviewAgent

_code_review = CodeReviewAgent()

def code_review_node(state):
    return _code_review.execute(state)
```

(A "node" is just a tiny function that runs one agent. The agent is created once
at the top and reused.)

### Step 4 — Wire it into the graph

Open `app/graph/graph.py`. Add the node and move the arrows so it runs after code
generation:

```python
graph.add_node("code_generator", nodes.code_generator_node)
graph.add_node("code_review", nodes.code_review_node)   # NEW

graph.add_edge(START, "code_generator")
graph.add_edge("code_generator", "code_review")         # CHANGED (was -> END)
graph.add_edge("code_review", END)                      # NEW tail
```

**Done.** Run the tests (Section 8). The pipeline now runs Code Generation, then
Code Review, automatically.

Repeat these 4 steps for each remaining agent: refactoring, debugging, unit_test,
documentation, security. Always slot the new agent in the correct pipeline
position and update the arrows.

---

## 7. How to run the project locally

Do this once to set up:

```powershell
# from the repository root
cd services/implementation

# 1. Create a virtual environment (isolated Python for this project)
python -m venv .venv

# 2. Turn it on
.\.venv\Scripts\Activate.ps1

# 3. Install the packages
pip install -r requirements.txt

# 4. Create your secrets file and paste in your real API key
copy .env.example .env
# then open .env and set ANTHROPIC_API_KEY=sk-ant-...
```

To start the server:

```powershell
uvicorn app.main:app --reload
```

Then open **http://127.0.0.1:8000/docs** in your browser. This is an automatic,
clickable UI for every endpoint — you can try `/implementation/start` right there
without writing any code.

- Health check: http://127.0.0.1:8000/health

---

## 8. How to test

Tests live in `app/tests/`. They use a **fake AI** (so they run instantly, for
free, without a real API key). Run them any time:

```powershell
.\.venv\Scripts\Activate.ps1
pytest app/tests/ -q
```

Every time you add an agent, add or update a test so we know the pipeline still
works. Copy `test_workflow.py` as your starting point — it shows how to stub the
LLM with `monkeypatch`.

---

## 9. The rules (please follow these)

These keep the codebase clean as the team grows:

1. **One agent = one job.** Don't make an agent do two things.
2. **Agents never call the AI SDK directly** — always go through
   `self.llm` (the gateway).
3. **Agents update only the state fields they own.** Don't overwrite another
   agent's output.
4. **No workflow logic inside agents.** The order lives in `graph.py`, not
   inside an agent.
5. **No agent logic inside the API layer.** `routes.py` only receives requests
   and returns responses.
6. **Outside tools (GitHub, Semgrep, etc.) live in `integrations/`** — agents
   call those wrappers, not the tools directly.
7. **Prompts live in `app/prompts/`** as `.md` files — one per agent. Don't
   hard-code long prompts inside agent code.
8. **Write a test** for every new agent.
9. **Never commit `.env`** or real API keys. Use `.env.example` as the template.

---

## 10. Quick cheat sheet — "where does X go?"

| I want to...                          | Edit this file / folder                 |
|---------------------------------------|-----------------------------------------|
| Add a new agent                       | `app/agents/<name>.py` (+ 3 more steps) |
| Change how the AI is told to behave   | `app/prompts/<name>.md`                 |
| Change the order agents run in        | `app/graph/graph.py`                    |
| Add a new field agents can share      | `app/graph/state.py`                    |
| Add a new HTTP endpoint               | `app/api/routes.py`                     |
| Change the AI model or settings       | `.env` (and `app/config/settings.py`)   |
| Change how we talk to the AI          | `app/services/llm_gateway.py`           |
| Connect an outside tool (GitHub, etc.)| `app/integrations/<tool>.py`            |
| Add a package/dependency              | `requirements.txt`                      |
| Add a test                            | `app/tests/test_<thing>.py`             |

---

## 11. What's built vs. what's next

**Built and working today:**
- Config, LLM gateway, base agent, shared state
- FastAPI app with `/health` and `/implementation/start`
- LangGraph workflow with the Code Generation agent wired end-to-end
- Tests that run the whole path with a fake AI

**Next (build in this order):**
1. Remaining agents — review, refactoring, debugging, unit_test, documentation,
   security (use the Section 6 recipe).
2. Conditional routing in `router.py` (e.g. if review fails, loop back to
   refactoring instead of moving on).
3. Background execution + a `GET /status/{projectId}` endpoint, because running
   7 AI calls in one request will be slow.
4. The `services/` helpers (artifact, workspace) and `integrations/` wrappers.

---

Questions? Start by reading `app/agents/code_generator.py` and
`app/graph/graph.py` together — they are small and show the whole pattern.
```
