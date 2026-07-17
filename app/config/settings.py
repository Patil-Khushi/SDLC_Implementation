"""Centralized configuration loaded from environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Service
    app_name: str = "implementation-service"
    log_level: str = "INFO"

    # LLM (Claude via Microsoft Foundry / Azure AI Foundry).
    # The gateway (app/services/llm_gateway.py) is the ONLY importer of `anthropic`;
    # it builds an AnthropicFoundry client from these. Provide the api key plus EITHER
    # the full base_url OR the bare resource name (the SDK expands a resource to
    # https://<resource>.services.ai.azure.com/anthropic/).
    anthropic_foundry_api_key: str = ""
    anthropic_foundry_base_url: str = ""
    anthropic_foundry_resource: str = ""
    # On Foundry the model is the DEPLOYMENT name, not the public Anthropic model id.
    llm_model: str = "claude-sonnet-4-6"
    llm_max_tokens: int = 16000
    # Adaptive thinking is supported on Claude 4.6+ deployments (beta on Foundry).
    # Set false if you point llm_model at a deployment that lacks extended thinking.
    llm_thinking: bool = True

    # GitHub publish (demo publish flow: create repo + push under this PAT's owner).
    # Lets the agent publish under an account the local `gh` CLI isn't logged into.
    # github_owner is optional — defaults to the token's own login.
    github_pat: str = ""
    github_owner: str = ""

    # Database (used later for workflow state)
    database_url: str | None = None

    # Workspace
    workspace_dir: str = "app/workspace"

    # exec-sandbox MCP server (integrations/executor.py :: MCPExecutor)
    sandbox_enabled: bool = False               # connect the executor in the app lifespan
    sandbox_mcp_url: str = "http://localhost:8080/mcp"
    sandbox_mcp_transport: str = "streamable_http"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
