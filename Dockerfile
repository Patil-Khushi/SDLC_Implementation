# services/implementation — FastAPI + LangGraph pipeline (DEVELOPER_GUIDE.md / CLAUDE.md).
#
# Joins the same `sandbox_internal` Docker network as exec-sandbox (see root docker-compose.yml)
# so MCPExecutor can dial http://exec-sandbox:8080/mcp directly — the sandbox's network is
# `internal: true` and cannot publish ports to the host, so this service must run as a container
# on that network rather than as a host process to reach it.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pyproject.toml .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
