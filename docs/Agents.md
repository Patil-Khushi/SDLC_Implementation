# SDLC Multi-Agent System — Agent Responsibilities, Flows & Tools

> **Scope:** POC-level, open-source-first. This document explains what each of the
> 7 agents does, how it works step-by-step, and every tool it uses — including
> beginner-friendly primers for tools the team is new to.

---

## Table of Contents

1. [Big Picture — How the Agents Fit Together](#big-picture)
2. [Shared Foundations (read this first)](#shared-foundations)
3. [Agent 1 — Code Generation](#agent-1--code-generation)
4. [Agent 2 — Code Review](#agent-2--code-review)
5. [Agent 3 — Refactoring](#agent-3--refactoring)
6. [Agent 4 — Debugging](#agent-4--debugging)
7. [Agent 5 — Unit Test Generator](#agent-5--unit-test-generator)
8. [Agent 6 — Documentation](#agent-6--documentation)
9. [Agent 7 — Security](#agent-7--security)
10. [Tool Glossary (every tool, plain English)](#tool-glossary)

---

<a name="big-picture"></a>
## 1. Big Picture — How the Agents Fit Together

The system takes a **Design Package** (requirements, architecture, data models,
API contracts) as input and produces a **working, reviewed, tested, documented,
and security-scanned codebase** as output.

The 7 agents run as a **pipeline orchestrated by LangGraph**. Each agent reads
from and writes to a **shared state** (a big Python dictionary/object that
travels through the graph), so later agents can see what earlier agents produced.

```
                          ┌─────────────────────┐
   Design Package  ──────▶│  1. Code Generation │  writes source code to repo
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │   2. Code Review    │  finds quality/standards issues
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │   3. Refactoring    │  improves structure (safely)
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │   4. Debugging      │  runs it, fixes errors  ◀──┐
                          └──────────┬──────────┘                            │
                                     │                              (loop until it runs)
                          ┌──────────▼──────────┐                            │
                          │ 5. Unit Test Gen    │  writes + runs tests  ─────┘
                          └──────────┬──────────┘   (failures can loop back to Debugging)
                                     │
                          ┌──────────▼──────────┐
                          │  6. Documentation   │  README, API docs, docstrings
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │   7. Security       │  vulnerability + dependency scan
                          └──────────┬──────────┘
                                     │
                                     ▼
                          Final reviewed project
```

**Key idea:** agents are not just "call the LLM." Each one pairs the **LLM
(the brain — reasons, writes, explains)** with **deterministic tools (the hands —
parse, lint, format, run, scan)**. The tools give ground truth; the LLM
interprets and acts on it. This is what makes the output trustworthy instead of
"hallucinated code that looks right."

---

<a name="shared-foundations"></a>
## 2. Shared Foundations (read this first)

These three things are used by **every** agent, so they're explained once here.

### 2.1 LangGraph — the orchestrator
- **What it is:** an open-source Python library for building **stateful, multi-step
  agent workflows** as a *graph*. You define **nodes** (each node = one agent or
  one step) and **edges** (what runs next, including conditional branches and loops).
- **Why we use it:** our 7 agents form a pipeline with loops (e.g. Debugging ↔ Testing
  retry until it passes). LangGraph handles "run node A, pass state to node B, loop
  back if condition X." Without it you'd hand-write all that control flow.
- **Mental model:** think of it as a flowchart where each box is a function that
  receives the shared `state`, does its work, and returns an updated `state`.

### 2.2 The Shared State
A single object passed between agents. Rough shape for the POC:
```python
state = {
    "design_package": {...},      # input requirements/architecture
    "repo_path": "c:/ISG/SDLC/generated_project",
    "generated_files": [...],     # paths written by Agent 1
    "review_findings": [...],     # from Agent 2
    "refactor_notes": [...],      # from Agent 3
    "run_logs": "...",            # from Agent 4
    "test_results": {...},        # from Agent 5
    "docs": {...},                # from Agent 6
    "security_report": {...},     # from Agent 7
    "language": "python" | "react",
}
```

### 2.3 The LLM provider (swappable)
- Every agent marked "LLM ✅" calls a large language model.
- **Options:** `anthropic` (Claude), `openai` (GPT) — paid APIs; or **`ollama`** —
  free, runs models locally on your machine (fully open source, no cost, no data leaves).
- **Recommendation:** hide the provider behind **one interface/function** (e.g.
  `call_llm(prompt) -> str`) so you can switch Claude ↔ Ollama by changing one config
  line. Do this on day 1.

---

<a name="agent-1--code-generation"></a>
## 3. Agent 1 — Code Generation

### Responsibility
Turn the **Design Package** into actual project **source code** and write it into
a repository. This is the agent that "builds the app."

It handles two kinds of output:
- **Non-deterministic code** (business logic, endpoints, components) → generated by the **LLM**.
- **Deterministic boilerplate** (folder structure, config files, `requirements.txt`,
  `package.json`, Dockerfiles) → generated from **templates** so it's consistent every time.

### Inputs → Outputs
- **In:** `design_package`, target `language`.
- **Out:** files written to `repo_path`; list of `generated_files` in state.

### Flow (step by step)
1. **Parse the design package** — extract modules, entities, endpoints, tech stack.
2. **Scaffold the project skeleton** — use **Jinja2 templates** to create folders and
   boilerplate files (deterministic, no LLM needed).
3. **Generate logic files** — for each module, prompt the **LLM** with the relevant
   design slice and ask for the implementation.
4. **Write files to disk / repo** — use **GitPython** to write files and make an
   initial commit (so every later change is tracked and reversible).
5. **Update state** with the list of generated files.

### Tools
| Tool | Role here | New-to-us? |
|------|-----------|------------|
| **LLM** | Writes the actual code from the design | — |
| **LangGraph** | Runs this as the first node | — |
| **Jinja2** | Fills in template files (configs, folder scaffolding) with your project values | ⭐ see glossary |
| **GitPython** | Writes files & commits them to the repo from Python | ⭐ see glossary |

> **Why Jinja2 + LLM together?** LLMs are great at logic but waste tokens and
> vary on repetitive boilerplate. Templates guarantee the same clean `package.json`
> / config every time; the LLM is saved for the parts that need real reasoning.

---

<a name="agent-2--code-review"></a>
## 4. Agent 2 — Code Review

### Responsibility
Review the generated code for **quality, coding standards, and best practices** —
the way a senior engineer would in a pull request. It does **not** change code; it
**reports findings** (later agents act on them).

### Inputs → Outputs
- **In:** `generated_files`, `language`.
- **Out:** `review_findings` — a structured list of issues (file, line, severity, message).

### Flow (step by step)
1. **Run linters** on the code:
   - Python → **Ruff**
   - React/TypeScript → **ESLint**
   These produce a precise, machine-readable list of real issues (unused imports,
   style violations, likely bugs).
2. **Collect linter output** into a normalized findings list.
3. **LLM pass** — feed the code + linter findings to the LLM and ask it to:
   - explain *why* each issue matters (plain English),
   - catch higher-level problems linters can't (bad naming, poor separation of
     concerns, missing error handling).
4. **Merge & rank** findings by severity into `review_findings`.

### Tools
| Tool | Role here | New-to-us? |
|------|-----------|------------|
| **Ruff** | Fast Python linter — finds issues & style problems | ⭐ see glossary |
| **ESLint** | Standard JS/TS/React linter | ⭐ see glossary |
| **LLM** | Explains issues & finds design-level problems linters miss | — |

> **POC note:** we dropped **Tree-sitter** here. Ruff and ESLint already parse the
> code and give structured results — Tree-sitter would only be needed later if you
> write *custom* structural rules.

---

<a name="agent-3--refactoring"></a>
## 5. Agent 3 — Refactoring

### Responsibility
Improve the **structure and readability** of the code **without changing what it
does**. Examples: split a huge function, remove duplication, rename for clarity,
apply consistent formatting.

### Inputs → Outputs
- **In:** `generated_files`, `review_findings` (uses review hints).
- **Out:** refactored files on disk; `refactor_notes` describing changes.

### Flow (step by step)
1. **LLM proposes refactorings** — given the code and review findings, ask the LLM
   for improved versions (with a strict instruction: *behavior must stay identical*).
2. **Apply the changes** to the files.
3. **Format the code** to a clean, consistent style:
   - Python → **Black**
   - React/TS → **Prettier**
4. **Safety check (important):** because "refactor = no behavior change" is easy to
   get wrong, the refactored code should be **re-tested by Agent 5**. If tests fail,
   the change gets reverted or sent to Debugging.
5. **Record `refactor_notes`.**

### Tools
| Tool | Role here | New-to-us? |
|------|-----------|------------|
| **LLM** | Proposes the structural improvements | — |
| **Black** | Auto-formats Python to one canonical style | ⭐ see glossary |
| **Prettier** | Auto-formats JS/TS/React to one canonical style | ⭐ see glossary |

> **Note:** Black and Prettier only *format* (whitespace, layout) — they do **not**
> refactor logic. The LLM does the actual restructuring; the formatters make the
> result tidy. Tree-sitter is again optional/later-only.

---

<a name="agent-4--debugging"></a>
## 6. Agent 4 — Debugging

### Responsibility
Actually **run the generated application**, capture any **compile/runtime errors**,
and **fix them** — repeating until the app runs cleanly.

### Inputs → Outputs
- **In:** generated (and refactored) project.
- **Out:** working code; `run_logs`. Loops until success or a retry limit.

### Flow (step by step)
1. **Run the app / build** using a **Python `subprocess`** call (e.g. `python main.py`,
   `npm run build`). Capture **stdout + stderr**.
2. **Detect failure** — non-zero exit code or error text in logs.
3. **LLM analyzes the logs** — send the error output + relevant source file to the
   LLM and ask for a diagnosis and a concrete fix.
4. **Apply the fix**, then **go back to step 1** (this is a LangGraph loop).
5. **Stop** when the app runs cleanly **or** a max-retry count is hit (so it can't
   loop forever).

### Tools
| Tool | Role here | New-to-us? |
|------|-----------|------------|
| **Python subprocess** | Runs the app/build and captures output | ⭐ see glossary |
| **LLM** | Reads the error logs and proposes the fix | — |
| **Docker** *(optional/later)* | Runs the app in an isolated container | ⭐ see glossary |

> **POC note:** we start with plain **subprocess** (simplest, no setup). Add
> **Docker** later only when you need clean isolation or to match a production
> runtime — it's the heaviest tool in the stack.

---

<a name="agent-5--unit-test-generator"></a>
## 7. Agent 5 — Unit Test Generator

### Responsibility
**Generate unit tests** for the code and **run them**, producing a pass/fail report.
This is also what verifies that Refactoring (Agent 3) and Debugging (Agent 4) didn't
break anything.

### Inputs → Outputs
- **In:** source files, `language`.
- **Out:** test files; `test_results` (passed/failed counts, failures detail).

### Flow (step by step)
1. **LLM writes tests** — for each module/function, prompt the LLM to generate unit
   tests covering normal cases and edge cases.
2. **Write test files** into the project's test folder.
3. **Run the tests**:
   - Python → **pytest**
   - React → **Jest**
4. **Collect results.** If tests **fail**, either:
   - loop the failure back to **Agent 4 (Debugging)**, or
   - ask the LLM to fix the test if the test itself was wrong.
5. **Store `test_results`.**

### Tools
| Tool | Role here | New-to-us? |
|------|-----------|------------|
| **LLM** | Generates the test cases | — |
| **pytest** | Runs Python tests, gives a report | ⭐ see glossary |
| **Jest** | Runs React/JS tests, gives a report | ⭐ see glossary |

---

<a name="agent-6--documentation"></a>
## 8. Agent 6 — Documentation

### Responsibility
Produce human-facing docs: a **README**, **inline documentation/docstrings**, and
(if applicable) **API documentation**.

### Inputs → Outputs
- **In:** final source code, `design_package`.
- **Out:** `README.md`, docstrings added to code, optional `openapi.json`; `docs` in state.

### Flow (step by step)
1. **LLM generates the README** — project overview, setup steps, usage, based on the
   code and design package.
2. **LLM adds inline docstrings/comments** to functions and modules.
3. **API docs (conditional):** *if the generated backend uses FastAPI*, export the
   **OpenAPI spec** automatically (FastAPI generates it for free from the code). If
   it's not FastAPI, skip this step.
4. **Save all docs** and update state.

### Tools
| Tool | Role here | New-to-us? |
|------|-----------|------------|
| **LLM** | Writes README, docstrings, explanations | — |
| **FastAPI OpenAPI** *(conditional)* | Auto-exports API spec — **only if the app is FastAPI** | ⭐ see glossary |

> **POC note:** FastAPI OpenAPI is **not** a general documentation tool — it only
> applies when the generated backend is a FastAPI app. Treat it as app-specific,
> not a core dependency.

---

<a name="agent-7--security"></a>
## 9. Agent 7 — Security

### Responsibility
Find **security vulnerabilities in the code** and **known-vulnerable dependencies**,
then report them (optionally explained in plain English).

### Inputs → Outputs
- **In:** final source code + dependency files (`requirements.txt`, `package.json`).
- **Out:** `security_report` — code vulnerabilities + risky dependencies.

### Flow (step by step)
1. **Static code analysis** — run **Semgrep** across the source to find insecure
   patterns (SQL injection, hardcoded secrets, unsafe calls, etc.).
2. **Dependency audit:**
   - Python → **pip-audit** (checks installed packages against a vulnerability database)
   - Node/React → **npm audit** (built into npm)
3. **Collect findings** into a structured report.
4. **LLM (optional) summarizes** the findings in plain English and suggests fixes.

### Tools
| Tool | Role here | New-to-us? |
|------|-----------|------------|
| **Semgrep** | Static analysis — pattern-based vulnerability scanning | ⭐ see glossary |
| **pip-audit** | Flags known-vulnerable Python dependencies | ⭐ see glossary |
| **npm audit** | Flags known-vulnerable Node dependencies (built into npm) | ⭐ see glossary |
| **LLM** *(optional)* | Turns raw findings into readable explanations | — |

---

<a name="tool-glossary"></a>
## 10. Tool Glossary (every tool, plain English)

For tools the team is new to — what it is, why it exists, and how to run it.

### LangGraph
- **What:** Python library to build multi-step, stateful agent workflows as a graph
  of nodes and edges (supports branches and loops).
- **Why:** orchestrates our 7 agents and the retry loops between them.
- **Install:** `pip install langgraph`

### Jinja2
- **What:** a **templating engine**. You write a template file with placeholders
  (`{{ project_name }}`, `{% for x in items %}`) and Jinja2 fills them with real
  values to produce a final file.
- **Why:** generate consistent boilerplate (configs, folder scaffolding) deterministically.
- **Install:** `pip install jinja2`
- **Tiny example:** template `Hello {{ name }}` + `{name: "SDLC"}` → `Hello SDLC`.

### GitPython
- **What:** a Python wrapper around Git — lets you create repos, add/commit files,
  branch, etc. from Python code instead of the command line.
- **Why:** Agent 1 writes generated files and commits them, so every later change
  is tracked and reversible.
- **Install:** `pip install gitpython`

### Ruff
- **What:** an extremely fast **Python linter** (and formatter). Finds unused imports,
  style issues, and likely bugs.
- **Why:** gives Agent 2 precise, structured code issues instantly.
- **Install:** `pip install ruff` — **Run:** `ruff check .`

### ESLint
- **What:** the standard **JavaScript/TypeScript/React linter**. Configurable rules
  for code quality and style.
- **Why:** Agent 2's reviewer for front-end code.
- **Install:** `npm install --save-dev eslint` — **Run:** `npx eslint .`

### Black
- **What:** the opinionated **Python code formatter** ("any color you like, as long
  as it's black"). One canonical style, zero config.
- **Why:** Agent 3 formats refactored Python cleanly.
- **Install:** `pip install black` — **Run:** `black .`

### Prettier
- **What:** the standard **JS/TS/React/CSS/JSON formatter**. Consistent layout.
- **Why:** Agent 3 formats refactored front-end code.
- **Install:** `npm install --save-dev prettier` — **Run:** `npx prettier --write .`

### Python subprocess
- **What:** a built-in Python module (no install) to run external commands/programs
  and capture their output and exit code.
- **Why:** Agent 4 uses it to actually run the app/build and read the errors.
- **Example:**
  ```python
  import subprocess
  r = subprocess.run(["python", "main.py"], capture_output=True, text=True)
  print(r.stdout, r.stderr, r.returncode)
  ```

### Docker *(optional/later)*
- **What:** packages an app + its environment into an isolated **container** that
  runs the same everywhere.
- **Why:** clean, repeatable runtime for Agent 4 — but heavier to set up. Start
  without it for the POC.
- **Install:** Docker Desktop (Windows).

### pytest
- **What:** the most popular **Python testing framework**. Simple test functions,
  rich reports.
- **Why:** Agent 5 runs generated Python tests.
- **Install:** `pip install pytest` — **Run:** `pytest`

### Jest
- **What:** the standard **JavaScript/React testing framework** from the JS ecosystem.
- **Why:** Agent 5 runs generated front-end tests.
- **Install:** `npm install --save-dev jest` — **Run:** `npx jest`

### FastAPI OpenAPI *(conditional)*
- **What:** FastAPI (a Python web framework) **auto-generates an OpenAPI/Swagger spec**
  describing all your API endpoints — for free, from your code.
- **Why:** Agent 6 exports API docs **only if** the generated backend is FastAPI.
- **Access:** a running FastAPI app serves it at `/openapi.json` and `/docs`.

### Semgrep
- **What:** an open-source **static analysis** tool that scans source code for
  patterns — including security vulnerabilities — using readable rules.
- **Why:** Agent 7's core code-vulnerability scanner.
- **Install:** `pip install semgrep` — **Run:** `semgrep --config auto .`

### pip-audit
- **What:** scans your Python dependencies against a database of **known
  vulnerabilities** (CVEs) and reports risky packages.
- **Why:** Agent 7's Python dependency check.
- **Install:** `pip install pip-audit` — **Run:** `pip-audit`

### npm audit
- **What:** built into npm — checks your Node dependencies for known vulnerabilities.
- **Why:** Agent 7's front-end dependency check.
- **Run:** `npm audit` (no install needed)

---

## Appendix — One-line summary per agent

1. **Code Generation** — LLM + Jinja2 write the code, GitPython commits it.
2. **Code Review** — Ruff/ESLint find issues, LLM explains & finds design flaws.
3. **Refactoring** — LLM restructures, Black/Prettier format, tests verify safety.
4. **Debugging** — subprocess runs it, LLM fixes errors, loop until clean.
5. **Unit Test Generator** — LLM writes tests, pytest/Jest run them.
6. **Documentation** — LLM writes README/docstrings, FastAPI exports API spec (if applicable).
7. **Security** — Semgrep + pip-audit/npm audit scan, LLM summarizes.

*All tools are open source; the only paid part is the LLM API — use Ollama to keep it 100% free.*
