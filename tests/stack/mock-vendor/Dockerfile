# Built from the repo root so it installs a hash-checked lock (the gateway's
# lock carries everything the stub needs: FastAPI, uvicorn, PyJWT, multipart).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
WORKDIR /app
COPY requirements-gateway.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-gateway.lock
COPY tests/stack/mock-vendor/main.py .
USER 65534
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8310"]
