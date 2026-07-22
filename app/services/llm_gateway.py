"""Centralized LLM communication.

All agents call the LLM through this gateway — never the provider SDK directly.
This keeps prompt execution, retries, logging, and provider choice in one place.
"""

import logging
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any, TypeVar

import anthropic

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


def _retry_after_seconds(exc: Exception, default: float) -> float:
	"""How long to wait before retrying a 429, from the ``Retry-After`` header or the error body.

	Foundry/Azure returns a ``retry-after`` header AND embeds "Please wait N seconds" in the
	message; honor whichever is present (capped) so we back off exactly as long as the API asks."""
	resp = getattr(exc, "response", None)
	if resp is not None:
		try:
			header = resp.headers.get("retry-after")
		except Exception:  # noqa: BLE001 - defensive: headers shape can vary
			header = None
		if header:
			try:
				return min(float(header) + 1.0, 60.0)
			except ValueError:
				pass
	match = re.search(r"wait\s+(\d+)\s*second", str(exc), re.IGNORECASE)
	if match:
		return min(float(match.group(1)) + 1.0, 60.0)
	return default


def _truncate(text: str, limit: int = 200) -> str:
	"""Shorten a log value so a tool result (e.g. a whole file's content) doesn't flood the log."""
	text = text.replace("\n", "\\n")
	return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def _summarize_tool_input(tool_input: Any) -> str:
	"""Render a tool call's arguments for a log line, eliding a bulky ``content`` field (the
	``write_file`` tool's full corrected file text) so the log stays readable."""
	if not isinstance(tool_input, dict):
		return _truncate(str(tool_input))
	parts = []
	for key, value in tool_input.items():
		if key == "content" and isinstance(value, str):
			parts.append(f"content=<{len(value)} chars>")
		else:
			parts.append(f"{key}={_truncate(str(value), 80)}")
	return ", ".join(parts)


_T = TypeVar("_T")


def _with_rate_limit_retry(call: Callable[[], _T], *, max_attempts: int = 6) -> _T:
	"""Run ``call`` and retry on ``anthropic.RateLimitError`` (429), honoring the API's requested
	wait, up to ``max_attempts`` total tries. Shared by :meth:`LLMGateway.complete` (streaming) and
	:meth:`LLMGateway.complete_with_tools` (the repair path's tool-use loop) — chunked generation
	fires many calls per feature, and either path can hit the per-minute rate limit in a burst."""
	for attempt in range(1, max_attempts + 1):
		try:
			return call()
		except anthropic.RateLimitError as exc:
			if attempt == max_attempts:
				raise
			wait = _retry_after_seconds(exc, default=15.0)
			logger.warning(
				"llm_call 429 rate-limited; waiting %.0fs then retrying (attempt %d/%d)",
				wait, attempt, max_attempts,
			)
			time.sleep(wait)
	raise AssertionError("unreachable")  # loop always returns or raises


class LLMGateway:
	"""Thin wrapper over the Anthropic Messages API."""

	def __init__(self) -> None:
		settings = get_settings()
		self._model = settings.llm_model
		self._max_tokens = settings.llm_max_tokens
		self._api_key = settings.anthropic_foundry_api_key or None
		self._base_url = settings.anthropic_foundry_base_url or None
		self._resource = settings.anthropic_foundry_resource or None
		self._timeout = settings.llm_timeout_seconds
		self._use_thinking = settings.llm_thinking
		# Build the client lazily (see _get_client). The Anthropic SDK raises
		# at construction if no credentials/endpoint are resolvable, so building
		# it here would make merely importing this module require Foundry config —
		# breaking app boot and tests when none is set.
		self._client: anthropic.AnthropicFoundry | None = None

	def _get_client(self) -> anthropic.AnthropicFoundry:
		"""Create the Foundry client on first use (Claude via Azure AI Foundry)."""
		if self._client is None:
			kwargs: dict = {"api_key": self._api_key, "timeout": self._timeout}
			# base_url and resource are mutually exclusive in the SDK; prefer base_url.
			if self._base_url:
				kwargs["base_url"] = self._base_url
			elif self._resource:
				kwargs["resource"] = self._resource
			self._client = anthropic.AnthropicFoundry(**kwargs)
		return self._client

	def complete(
		self,
		prompt: str,
		*,
		system: str | None = None,
		max_tokens: int | None = None,
	) -> str:
		"""Run a single prompt and return the model's text response."""
		kwargs: dict = {
			"model": self._model,
			"max_tokens": max_tokens or self._max_tokens,
			"system": system or anthropic.NOT_GIVEN,
			"messages": [{"role": "user", "content": prompt}],
		}
		# Adaptive thinking is only supported on Claude 4.6+ models. Disable it
		# via LLM_THINKING=false if LLM_MODEL points at a model without it.
		if self._use_thinking:
			kwargs["thinking"] = {"type": "adaptive"}

		# Stream and accumulate rather than a single blocking create(): large outputs (code
		# generation can be tens of thousands of tokens) otherwise exceed the non-streaming HTTP
		# timeout window. Streaming lets max_tokens go as high as the model supports (128K on
		# Sonnet 4.6) without timing out. 429s are retried by _with_rate_limit_retry (shared with
		# complete_with_tools) — chunked generation fires many calls, so the per-minute limit is
		# easy to hit in bursts; backing off and retrying makes a run resilient instead of failing.
		def _stream_once() -> Any:
			with self._get_client().messages.stream(**kwargs) as stream:
				return stream.get_final_message()

		response = _with_rate_limit_retry(_stream_once)
		logger.info(
			"llm_call model=%s input_tokens=%s output_tokens=%s stop=%s",
			self._model,
			response.usage.input_tokens,
			response.usage.output_tokens,
			response.stop_reason,  # 'max_tokens' here means the reply was truncated
		)
		return "".join(
			block.text for block in response.content if block.type == "text"
		)

	def complete_with_tools(
		self,
		prompt: str,
		*,
		system: str | None = None,
		tools: list | None = None,
		max_iters: int = 4,
	) -> str:
		"""Tool-augmented completion: bind ``tools`` to the model and run a tool-use loop.

		The tool binding + provider SDK usage live HERE (the single door) so callers like the
		repair node never import the SDK. Each tool may be a repair-tool wrapper (with a
		``handler``) or a LangChain tool (with ``invoke``). Returns the model's final text
		(which carries the proposed fix). Falls back to :meth:`complete` when no tools are given.
		"""
		if not tools:
			return self.complete(prompt, system=system)

		client = self._get_client()
		specs = [self._tool_spec(tool) for tool in tools]
		by_name = {getattr(tool, "name", ""): tool for tool in tools}
		messages: list = [{"role": "user", "content": prompt}]
		final_text = ""
		for turn in range(1, max_iters + 1):
			kwargs: dict = {
				"model": self._model,
				"max_tokens": self._max_tokens,
				"messages": messages,
				"tools": specs,
			}
			if system:
				kwargs["system"] = system
			# Same 429 backoff as complete() — see _with_rate_limit_retry. Not streamed: proposed
			# fixes are small compared to a full-module generation, so the blocking HTTP timeout
			# is not the risk here; the rate limit from repeated repair/tool-loop calls is.
			response = _with_rate_limit_retry(lambda: client.messages.create(**kwargs))
			final_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
			tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
			logger.info(
				"tool_loop turn=%d/%d input_tokens=%s output_tokens=%s stop=%s tool_calls=%s",
				turn, max_iters, response.usage.input_tokens, response.usage.output_tokens,
				response.stop_reason, [tu.name for tu in tool_uses] or "(none)",
			)
			if not tool_uses:
				return final_text
			messages.append({"role": "assistant", "content": response.content})
			results = []
			for tu in tool_uses:
				logger.info("  tool_call %s(%s)", tu.name, _summarize_tool_input(tu.input))
				outcome = self._run_tool(by_name.get(tu.name), tu.input)
				logger.info("  tool_result %s -> %s", tu.name, _truncate(str(outcome)))
				results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(outcome)})
			messages.append({"role": "user", "content": results})
		logger.warning("tool_loop exhausted max_iters=%d without a final (tool-free) reply", max_iters)
		return final_text

	@staticmethod
	def _tool_spec(tool: Any) -> dict:
		"""Build an Anthropic tool spec from a repair-tool wrapper or a LangChain tool.

		A RepairTool carries ``input_schema`` as a plain JSON-schema dict. A LangChain tool
		instead exposes it as a pydantic model *class* (via ``input_schema``/``args_schema``);
		call ``model_json_schema()`` to get a serializable schema — putting the class itself
		into the request body raises "ModelMetaclass is not JSON serializable".
		"""
		schema = getattr(tool, "input_schema", None)
		if not isinstance(schema, dict):
			model = getattr(tool, "args_schema", None) or schema
			try:
				schema = model.model_json_schema()
			except Exception:  # noqa: BLE001
				schema = {"type": "object", "properties": {}}
		return {"name": getattr(tool, "name", ""), "description": getattr(tool, "description", ""), "input_schema": schema}

	@staticmethod
	def _run_tool(tool: Any, tool_input: Any) -> Any:
		"""Dispatch a model tool-call to the underlying tool implementation.

		A tool that raises (e.g. ``read_file`` on a path that doesn't exist, or a failed
		``run_command``) must NOT crash the run: the tool loop is the model INSPECTING the
		workspace, so a failure is ordinary feedback. Return the error as the tool_result content
		so the model can recover — mirroring how a shell reports an error back to a human — instead
		of letting the exception propagate up and abort the whole graph.
		"""
		if tool is None:
			return "unknown tool"
		try:
			if hasattr(tool, "handler"):
				return tool.handler(**tool_input) if isinstance(tool_input, dict) else tool.handler(tool_input)
			if hasattr(tool, "invoke"):
				return tool.invoke(tool_input)
		except Exception as exc:  # noqa: BLE001 - surface as tool feedback, never crash the tool loop
			return f"Error running tool {getattr(tool, 'name', '?')!r}: {type(exc).__name__}: {exc}"
		return "tool is not callable"


# Module-level singleton so agents share one client / connection pool.
llm_gateway = LLMGateway()


class FakeLLMGateway(LLMGateway):
	"""Deterministic test double with the same ``.complete(prompt, system=...)`` surface.

	Subclasses :class:`LLMGateway` (so it can be injected wherever one is expected) but does NOT
	call the real ``__init__`` — no settings, API key, or network. Configure with a list of
	responses (returned in order) OR a callable ``(prompt) -> str``. Every call is recorded in
	:attr:`calls`. Used by tests / conftest (DEVELOPER_GUIDE.md §8).
	"""

	def __init__(  # noqa: D401  # intentionally does not call super().__init__()
		self,
		responses: list[str] | Callable[[str], str] | None = None,
		*,
		default: str | None = None,
	) -> None:
		self._responder: Callable[[str], str] | None = responses if callable(responses) else None
		self._queue: deque[str] = deque([] if callable(responses) else (responses or []))
		self._default = default
		self.calls: list[dict[str, Any]] = []

	def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
		self.calls.append({"prompt": prompt, "system": system, "kwargs": kwargs})
		if self._responder is not None:
			return self._responder(prompt)
		if self._queue:
			return self._queue.popleft()
		if self._default is not None:
			return self._default
		raise IndexError("FakeLLMGateway ran out of scripted responses and no default was set")

	def complete_with_tools(
		self,
		prompt: str,
		*,
		system: str | None = None,
		tools: list | None = None,
		max_iters: int = 4,
	) -> str:
		# Deterministic double: ignore tools, return the next scripted response.
		# TODO: to catch accidental misuse (repair path calling the wrong method), tests could
		# script a distinct response here vs. complete() and assert which one was served.
		return self.complete(prompt, system=system)
