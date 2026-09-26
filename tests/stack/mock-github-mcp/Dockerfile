# Built from the repo root so it installs the gateway's hash-checked lock:
# the stand-in speaks MCP through the same pinned FastMCP.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
WORKDIR /app
COPY requirements-gateway.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-gateway.lock
COPY tests/stack/mock-github-mcp/main.py .
USER 65534
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8330"]
