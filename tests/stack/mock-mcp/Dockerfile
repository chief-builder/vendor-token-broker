# Built from the repo root so it installs the gateway's hash-checked lock:
# the stand-in speaks MCP through the same pinned FastMCP.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
WORKDIR /app
COPY requirements-gateway.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-gateway.lock
COPY tests/stack/mock-mcp/main.py .
USER 65534
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8330"]
