FROM python:3.12-slim
RUN pip install --no-cache-dir 'fastapi==0.115.*' 'uvicorn==0.32.*' 'python-multipart==0.0.*'
WORKDIR /app
COPY main.py .
USER 65534
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8310"]
