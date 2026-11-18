# Base image pinned by digest (python:3.14-slim); dependencies installed
# from the hash-checked lock, then the package itself without dependencies.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
USER 65534
CMD ["python", "-m", "uvicorn", "--factory", "token_broker.main:create_app", \
     "--host", "0.0.0.0", "--port", "8300"]
