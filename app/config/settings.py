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
    # A work item is now one whole module/directory (several files in one reply), not a single
    # endpoint, so each generation returns more code — give it enough output headroom.
    llm_max_tokens: int = 32000
    # Per-request wall-clock budget (seconds) for a single LLM call. Larger module items take
    # longer to generate; without this the SDK default could cut a slow reply short. Passed to
    # the Anthropic client in llm_gateway.py.
    llm_timeout_seconds: float = 600.0
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

    # Code Review: clones the generated PUBLIC repo into an EPHEMERAL sandbox, runs static
    # analysis (ruff/eslint + sonar-scanner) inside it, tears it down, then writes a Markdown
    # report. No code is executed (Testing does that); no repo token is needed (public repos).
    review_sandbox_image: str = "sdlc-review-sandbox:latest"  # image w/ git+ruff+node/eslint+sonar-scanner
    review_sandbox_timeout: float = 900.0                     # hard cap on the whole session (s)
    reports_dir: str = "reports"                              # where <project>-<run>.md reports land
    # Git working model: all phases work on this branch; it is merged -> main only after the
    # Security scan passes (final step).
    working_branch: str = "dev"

    # SonarQube static analysis (integrations/sonarqube.py). OFF by default; when disabled the
    # review runs on ruff/eslint + the LLM alone. NOTE two URLs for the same server:
    #   * sonarqube_scanner_url — used by sonar-scanner INSIDE the sandbox container (upload)
    #   * sonarqube_url         — used by the agent ON THE HOST to read issues back
    sonarqube_enabled: bool = False
    sonarqube_url: str = "http://localhost:9000"                 # host-side read (published port)
    sonarqube_scanner_url: str = "http://host.docker.internal:9000"  # sandbox-side upload
    sonarqube_token: str = ""                   # user/analysis token (Bearer auth)
    sonarqube_project_key: str = ""             # component key to scan + pull issues for
    sonarqube_timeout: float = 30.0             # HTTP timeout in seconds


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
