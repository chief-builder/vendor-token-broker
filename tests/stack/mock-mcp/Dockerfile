# Built from the repo root so it installs the gateway's hash-checked lock:
# the stand-in speaks MCP through the same pinned FastMCP.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
WORKDIR /app
COPY requirements-gateway.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-gateway.lock
COPY tests/stack/mock-mcp/main.py .
USER 65534
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8330"]
