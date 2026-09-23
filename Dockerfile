# Base image pinned by digest (python:3.12-slim); dependencies installed
# from the hash-checked lock, then the package itself without dependencies.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
USER 65534
CMD ["python", "-m", "uvicorn", "--factory", "token_broker.main:create_app", \
     "--host", "0.0.0.0", "--port", "8300"]
