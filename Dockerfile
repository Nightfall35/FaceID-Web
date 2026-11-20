FROM python:3.12-slim-bookworm
# Non-root UID 1000 (required by Hugging Face Spaces, harmless elsewhere)
RUN apt-get update && apt-get install -y --no-install-recommends curl libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* && useradd -m -u 1000 user && mkdir -p /data && chown user:user /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FACEID_DATA_DIR=/data FACEID_MODEL_DIR=/app/models
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the models into the image so cold starts never depend on GitHub.
COPY scripts/download_models.sh scripts/download_models.sh
RUN ./scripts/download_models.sh
COPY app app
USER user
VOLUME /data
EXPOSE 8000
# docker run -p 8000:8000 -e FACEID_PASSWORD=secret -e FACEID_SECRET=$(openssl rand -hex 32) -v faceid-data:/data faceid-web
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
