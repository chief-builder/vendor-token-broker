FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir '.[redis]'
USER 65534
CMD ["python", "-m", "uvicorn", "--factory", "token_broker.main:create_app", \
     "--host", "0.0.0.0", "--port", "8300"]
