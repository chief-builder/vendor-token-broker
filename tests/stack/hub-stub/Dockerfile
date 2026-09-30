# Built from the repo root so it installs a hash-checked lock (the gateway's
# lock carries everything the stub needs: FastAPI, uvicorn, PyJWT, multipart).
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
WORKDIR /app
COPY requirements-gateway.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-gateway.lock
COPY tests/stack/hub-stub/main.py .
USER 65534
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8320"]
