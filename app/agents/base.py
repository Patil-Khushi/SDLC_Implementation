"""Common base class for all agents.

Each agent implements a single responsibility and follows the interface:

    execute(state) -> state

Agents receive the shared workflow state, do their work (calling the LLM only
through the gateway), update the fields they own, and return the state.
"""

from abc import ABC, abstractmethod
from pathlib import Path

from app.graph.state import WorkflowState
from app.services.llm_gateway import llm_gateway

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class BaseAgent(ABC):
    """Base class providing shared access to the LLM gateway."""

    #: Human-readable name, used in logs and the workflow trace.
    name: str = "base"

    def __init__(self) -> None:
        self.llm = llm_gateway

    def _load_prompt(self, name: str) -> str:
        """Load a version-controlled prompt from app/prompts/<name>.md."""
        return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")

    @abstractmethod
    def execute(self, state: WorkflowState) -> WorkflowState:
        """Perform this agent's task and return the updated state."""
        raise NotImplementedError
