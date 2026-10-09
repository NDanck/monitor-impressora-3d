FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# 1 worker: estado em memoria e assinatura MQTT nao podem ser duplicados.
# PORT e definido pelo provedor (Render); 8000 localmente.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
