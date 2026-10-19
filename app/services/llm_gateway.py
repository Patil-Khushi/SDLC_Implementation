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
import httpx

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


#: Prompt-cache breakpoints a single call may set (the API allows 4; one is left for callers
#: that also cache a system prompt or tools).
_MAX_CACHE_BREAKPOINTS = 3
#: How many times one tool session may be told its reply was cut off at max_tokens before it is
#: returned as it is (two nudges: a third cut-off means the task, not the reply, is too big).
_MAX_CUTOFF_NUDGES = 2


def _prefix_segments(cache_prefix: str | list[str] | None) -> list[str]:
	segments = [cache_prefix] if isinstance(cache_prefix, str) else list(cache_prefix or [])
	return [s for s in segments if s]


def full_prompt(prompt: str, cache_prefix: str | list[str] | None = None) -> str:
	"""The whole user message as one string: the cacheable prefix segments, then ``prompt``.

	What a model without prompt caching receives, and what test doubles record, so a caller that
	moves its shared context into ``cache_prefix`` changes nothing about what the model reads."""
	return "\n\n".join([*_prefix_segments(cache_prefix), prompt])


def _cached_user_content(prompt: str, cache_prefix: str | list[str] | None) -> list[dict[str, Any]]:
	"""The user message as text blocks, a cache breakpoint closing each prefix segment.

	Segments run from most to least shared (the run's naming contract, then one work item's
	slices), so a call reuses the longest prefix any earlier call wrote. A segment below the
	model's minimum cacheable size is simply not cached; that is not an error."""
	segments = _prefix_segments(cache_prefix)
	cached, extra = segments[:_MAX_CACHE_BREAKPOINTS], segments[_MAX_CACHE_BREAKPOINTS:]
	blocks: list[dict[str, Any]] = [
		{"type": "text", "text": f"{s}\n\n", "cache_control": {"type": "ephemeral"}} for s in cached
	]
	blocks.append({"type": "text", "text": "\n\n".join([*extra, prompt])})
	return blocks


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


# --- transient-failure retry policy ---------------------------------------
#
# A run fires MANY LLM calls (one per file/module); a single transient blip must not discard all
# prior progress. So we retry the WHOLE transient set with backoff and let only PERMANENT errors
# surface. Retryability is decided by CLASS + HTTP STATUS (below), not by enumerating one concrete
# exception at a time — that way a new transient flavor (today a 529 "overloaded", tomorrow a 503
# or a mid-stream socket reset) is already covered without another patch.

# HTTP statuses worth retrying: 408 request-timeout, 409 conflict, 425 too-early, 429 rate-limit,
# plus ANY 5xx (500/502/503/504 and 529 "overloaded"). Every OTHER 4xx — 400 bad-request, 401/403
# auth, 404 not-found, 422 unprocessable — is a PERMANENT client error: retrying only burns
# time/tokens and hides the real bug, so it surfaces immediately.
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})

# Connection/transport failures carry no HTTP status and are always transient. anthropic wraps
# connect-time failures in APIConnectionError (incl. APITimeoutError); but once a response has
# STARTED streaming, a mid-stream drop surfaces as a RAW httpx error the SDK does NOT wrap.
# httpx.TransportError is the BASE of every connect/read/write/pool/timeout/protocol variant, so it
# covers them all — not just the handful (ReadError/WriteError/RemoteProtocolError) we hit before.
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
	anthropic.APIConnectionError,
	httpx.TransportError,
)

# Kept as an alias for older imports; the loop no longer keys off this tuple directly.
_TRANSIENT_ERRORS = _CONNECTION_ERRORS

# What the loop CATCHES: every anthropic SDK error + every raw httpx transport error. Whether a
# caught error is actually retried is decided by :func:`_retryable_reason`; this tuple only bounds
# what we inspect vs. let propagate untouched (KeyboardInterrupt/SystemExit are never caught).
_MAYBE_RETRYABLE: tuple[type[BaseException], ...] = (anthropic.AnthropicError, httpx.TransportError)


def _error_body_type(exc: BaseException) -> str:
	"""The ``error.type`` discriminator from an anthropic error body (e.g. ``'overloaded_error'``),
	or ``''`` — a fallback for a transient error whose HTTP status isn't a clean 5xx we can key on."""
	body = getattr(exc, "body", None)
	if isinstance(body, dict):
		err = body.get("error")
		if isinstance(err, dict):
			return str(err.get("type", ""))
	return ""


def _retryable_reason(exc: BaseException) -> str | None:
	"""A short reason string if ``exc`` is a TRANSIENT failure worth retrying, else ``None``.

	Retryable: any connection/timeout/transport drop; HTTP 408/409/425/429 or any 5xx (incl. 529
	``overloaded``); or an error body typed ``*overloaded*``/``*timeout*`` when no clean status is
	present. Non-retryable: every other 4xx (permanent client errors) and non-API SDK errors — they
	won't succeed on retry, so they surface immediately with their original traceback."""
	if isinstance(exc, _CONNECTION_ERRORS):
		return f"connection/{type(exc).__name__}"
	if isinstance(exc, anthropic.APIStatusError):
		status = getattr(exc, "status_code", None)
		if isinstance(status, int) and (status in _RETRYABLE_STATUS_CODES or status >= 500):
			return f"http {status}"
		etype = _error_body_type(exc)
		if "overloaded" in etype or "timeout" in etype:
			return etype
		return None  # permanent 4xx (400/401/403/404/422)
	return None  # non-status, non-connection SDK error (e.g. response-validation) — don't retry


def _backoff_seconds(exc: BaseException, attempt: int) -> float:
	"""Seconds to wait before the next attempt: honor an explicit Retry-After when the server sent
	one (429s carry it), else exponential backoff — 2, 4, 8, 16, … capped at 30s."""
	return _retry_after_seconds(exc, default=min(2.0 ** attempt, 30.0))


def _with_rate_limit_retry(call: Callable[[], _T], *, max_attempts: int = 6) -> _T:
	"""Run ``call``, retrying every TRANSIENT failure with backoff, up to ``max_attempts`` tries.

	Transient = any connection/timeout/network drop, HTTP 408/409/425/429, or any 5xx (incl. 529
	``overloaded``) — see :func:`_retryable_reason`. PERMANENT client errors (400/401/403/404/422)
	re-raise immediately so a real bug fails fast instead of looping. Shared by
	:meth:`LLMGateway.complete` (streaming) and :meth:`LLMGateway.complete_with_tools`; a run fires
	one call per file/module, so a single transient blip must not discard all prior progress."""
	for attempt in range(1, max_attempts + 1):
		try:
			return call()
		except _MAYBE_RETRYABLE as exc:
			reason = _retryable_reason(exc)
			if reason is None or attempt == max_attempts:
				raise
			wait = _backoff_seconds(exc, attempt)
			logger.warning(
				"llm_call transient error [%s] (%s); waiting %.0fs then retrying (attempt %d/%d)",
				reason, type(exc).__name__, wait, attempt, max_attempts,
			)
			time.sleep(wait)
	raise AssertionError("unreachable")  # loop always returns or raises


class LLMGateway:
	"""Thin wrapper over the Anthropic Messages API."""

	def __init__(self) -> None:
		settings = get_settings()
		self._model = settings.llm_model
		self._max_tokens = settings.llm_max_tokens
		# stop_reason of the most recent model response ('end_turn', 'tool_use', 'max_tokens' ...).
		# Read by agents that report on a call after the fact (the Debugging report records it):
		# 'max_tokens' is the only direct evidence that a reply was cut off rather than wrong.
		self.last_stop_reason: str | None = None
		self._api_key = settings.anthropic_foundry_api_key or None
		self._base_url = settings.anthropic_foundry_base_url or None
		self._resource = settings.anthropic_foundry_resource or None
		self._timeout = settings.llm_timeout_seconds
		self._use_thinking = settings.llm_thinking
		self._prompt_cache = bool(getattr(settings, "llm_prompt_cache", True))
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
		cache_prefix: str | list[str] | None = None,
	) -> str:
		"""Run a single prompt and return the model's text response.

		``cache_prefix`` is context that comes BEFORE ``prompt`` and that other calls send
		identically (one string, or segments from most to least shared). The model reads exactly
		``full_prompt(prompt, cache_prefix)``; with caching on, the prefix is marked cacheable so
		repeated calls pay for it once per few minutes rather than every time."""
		use_cache = self._prompt_cache and bool(_prefix_segments(cache_prefix))
		content: Any = _cached_user_content(prompt, cache_prefix) if use_cache else full_prompt(prompt, cache_prefix)
		kwargs: dict = {
			"model": self._model,
			"max_tokens": max_tokens or self._max_tokens,
			"system": system or anthropic.NOT_GIVEN,
			"messages": [{"role": "user", "content": content}],
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

		try:
			response = _with_rate_limit_retry(_stream_once)
		except anthropic.BadRequestError as exc:
			# A deployment without prompt caching rejects cache_control. Losing the cache costs
			# money; losing the run costs hours — so drop it for this process and carry on.
			if not (use_cache and "cache" in str(exc).lower()):
				raise
			logger.warning("prompt caching rejected by the deployment (%s); continuing without it", _truncate(str(exc)))
			self._prompt_cache = False
			kwargs["messages"] = [{"role": "user", "content": full_prompt(prompt, cache_prefix)}]
			response = _with_rate_limit_retry(_stream_once)
		self.last_stop_reason = getattr(response, "stop_reason", None)
		logger.info(
			"llm_call model=%s input_tokens=%s cache_read=%s cache_write=%s output_tokens=%s stop=%s",
			self._model,
			response.usage.input_tokens,
			getattr(response.usage, "cache_read_input_tokens", None),
			getattr(response.usage, "cache_creation_input_tokens", None),
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
		max_iters: int = 6,
		cache_prefix: str | list[str] | None = None,
	) -> str:
		"""Tool-augmented completion: bind ``tools`` to the model and run a tool-use loop.

		The tool binding + provider SDK usage live HERE (the single door) so callers like the
		repair node never import the SDK. Each tool may be a repair-tool wrapper (with a
		``handler``) or a LangChain tool (with ``invoke``). Returns the model's final text
		(which carries the proposed fix). Falls back to :meth:`complete` when no tools are given.

		Every turn re-sends the whole conversation, so with caching on, ``cache_prefix`` (context
		shared with other calls — see :meth:`complete`) is marked cacheable in the first message,
		and a rolling breakpoint follows the newest tool results: each turn re-reads everything
		before it from cache instead of paying for it again.
		"""
		if not tools:
			return self.complete(prompt, system=system, cache_prefix=cache_prefix)

		client = self._get_client()
		specs = [self._tool_spec(tool) for tool in tools]
		by_name = {getattr(tool, "name", ""): tool for tool in tools}
		use_cache = self._prompt_cache
		first: Any = _cached_user_content(prompt, cache_prefix) if use_cache else full_prompt(prompt, cache_prefix)
		messages: list = [{"role": "user", "content": first}]
		rolling: dict | None = None          # the tool_result block carrying the rolling breakpoint
		cutoffs = 0                          # turns cut off at max_tokens that were nudged, not returned
		final_text = ""

		def _create(call_kwargs: dict) -> Any:
			nonlocal use_cache
			try:
				return _with_rate_limit_retry(lambda: client.messages.create(**call_kwargs))
			except anthropic.BadRequestError as exc:
				if not (use_cache and "cache" in str(exc).lower()):
					raise
				logger.warning("prompt caching rejected by the deployment (%s); continuing without it", _truncate(str(exc)))
				self._prompt_cache = use_cache = False
				messages[0]["content"] = full_prompt(prompt, cache_prefix)
				for message in messages:
					for block in message["content"] if isinstance(message["content"], list) else []:
						if isinstance(block, dict):
							block.pop("cache_control", None)
				return _with_rate_limit_retry(lambda: client.messages.create(**call_kwargs))

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
			response = _create(kwargs)
			self.last_stop_reason = getattr(response, "stop_reason", None)
			final_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
			tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
			logger.info(
				"tool_loop turn=%d/%d input_tokens=%s output_tokens=%s stop=%s tool_calls=%s",
				turn, max_iters, response.usage.input_tokens, response.usage.output_tokens,
				response.stop_reason, [tu.name for tu in tool_uses] or "(none)",
			)
			cut_off = getattr(response, "stop_reason", None) == "max_tokens"
			if cut_off and cutoffs < _MAX_CUTOFF_NUDGES:
				# The reply hit the output cap. Returning it ends the session with nothing done (four
				# debugging rounds in a row did exactly that: a long analysis, cut off, zero edits), and
				# running a tool call whose input was cut mid-way would apply half a file. Say what
				# happened and ask for small, immediate edits instead.
				cutoffs += 1
				logger.warning("tool_loop turn=%d reply cut off at max_tokens (%d tool call(s)); nudging", turn, len(tool_uses))
				messages.append({"role": "assistant", "content": response.content})
				note = ("Your last reply was CUT OFF at the output limit, so it was not completed. Do not repeat the "
						"analysis. Apply the fix NOW with small tool calls: `edit_file` with a short exact snippet, "
						"one file at a time. Never rewrite a whole large file with write_file.")
				if tool_uses:
					results = [{"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
								"content": "Not run: your reply was cut off before this call was complete. " + note}
							   for tu in tool_uses]
					messages.append({"role": "user", "content": results})
				else:
					messages.append({"role": "user", "content": note})
				continue
			if not tool_uses:
				return final_text
			messages.append({"role": "assistant", "content": response.content})
			results = []
			for tu in tool_uses:
				logger.info("  tool_call %s(%s)", tu.name, _summarize_tool_input(tu.input))
				outcome = self._run_tool(by_name.get(tu.name), tu.input)
				logger.info("  tool_result %s -> %s", tu.name, _truncate(str(outcome)))
				results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(outcome)})
			if use_cache and results:
				# Move the rolling breakpoint to the newest turn (the API allows 4 breakpoints:
				# up to 2 in the prefix, this one, and one spare).
				if rolling is not None:
					rolling.pop("cache_control", None)
				rolling = results[-1]
				rolling["cache_control"] = {"type": "ephemeral"}
			messages.append({"role": "user", "content": results})

		# The model was STILL calling tools on the last allowed turn — every caller here (the
		# Debugging/Refactoring/repair agents) parses this method's return value as the proposed
		# fix, so returning the empty/irrelevant `final_text` from that last tool-calling turn
		# means "no valid fix parsed - wrote nothing": the whole (possibly rate-limited, multi-call)
		# investigation gets thrown away and the SAME failure recurs next attempt, burning the
		# local repair/debug cap without ever trying a fix. Force exactly ONE more turn with NO
		# tools available — the model can only reply in text, using everything it already
		# gathered — instead of silently discarding all of that.
		logger.warning(
			"tool_loop exhausted max_iters=%d without a final (tool-free) reply; forcing one", max_iters,
		)
		messages.append({
			"role": "user",
			"content": "You have used all available tool calls. Reply now with your final answer in "
			           "the required format - no more tool use is possible.",
		})
		final_kwargs: dict = {"model": self._model, "max_tokens": self._max_tokens, "messages": messages}
		if system:
			final_kwargs["system"] = system
		response = _create(final_kwargs)
		self.last_stop_reason = getattr(response, "stop_reason", None)
		return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")

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
		self.last_stop_reason: str | None = None

	def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
		# Record (and answer from) the WHOLE message the model would read — a cache_prefix is
		# part of the prompt, not a side channel.
		prompt = full_prompt(prompt, kwargs.get("cache_prefix"))
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
		max_iters: int = 6,
		cache_prefix: str | list[str] | None = None,
	) -> str:
		# Deterministic double: ignore tools, return the next scripted response.
		# TODO: to catch accidental misuse (repair path calling the wrong method), tests could
		# script a distinct response here vs. complete() and assert which one was served.
		return self.complete(prompt, system=system, cache_prefix=cache_prefix)


def complete_cached(
	gateway: LLMGateway, prompt: str, *, cache_prefix: str | list[str] | None, **kwargs: Any
) -> str:
	"""``gateway.complete`` with a cacheable prefix — falling back to one joined prompt for a
	stand-in that predates ``cache_prefix`` (see :func:`complete_with_tools_cached`)."""
	try:
		return gateway.complete(prompt=prompt, cache_prefix=cache_prefix, **kwargs)
	except TypeError as exc:
		if "cache_prefix" not in str(exc):
			raise
		return gateway.complete(prompt=full_prompt(prompt, cache_prefix), **kwargs)


def complete_with_tools_cached(
	gateway: LLMGateway, prompt: str, *, cache_prefix: str | list[str] | None, **kwargs: Any
) -> str:
	"""``gateway.complete_with_tools`` with a cacheable prefix — or, for a gateway stand-in that
	predates ``cache_prefix`` (tests and scripts patch the method with plain functions), the same
	text as one prompt. What the model reads is identical either way."""
	try:
		return gateway.complete_with_tools(prompt=prompt, cache_prefix=cache_prefix, **kwargs)
	except TypeError as exc:
		if "cache_prefix" not in str(exc):
			raise
		return gateway.complete_with_tools(prompt=full_prompt(prompt, cache_prefix), **kwargs)
