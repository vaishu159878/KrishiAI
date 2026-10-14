FROM python:3.12-slim

WORKDIR /app

# Run backend as non-root user for container security
RUN groupadd -r krishi && useradd -r -g krishi krishi

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R krishi:krishi /app

USER krishi

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
