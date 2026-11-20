FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FACEID_DATA_DIR=/data FACEID_MODEL_DIR=/app/models
RUN apt-get update && apt-get install -y --no-install-recommends curl libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/download_models.sh scripts/download_models.sh
RUN ./scripts/download_models.sh
COPY app app
VOLUME /data
EXPOSE 8000
# Set FACEID_PASSWORD (and ideally FACEID_SECRET) at runtime:
#   docker run -p 8000:8000 -e FACEID_PASSWORD=secret -v faceid-data:/data faceid-web
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
