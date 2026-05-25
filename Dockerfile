# ---------- basis ----------
FROM python:3.12-slim

# ---------- systeem‑libraries ----------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        libglib2.0-0 \
        libgl1 \
 && rm -rf /var/lib/apt/lists/*

# ---------- werkmap ----------
WORKDIR /app

# ---------- Python‑dependencies ----------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- projectbestanden ----------
COPY . .

# ---------- env & poort ----------
ENV FLASK_APP=dreamz_portal.py \
    FLASK_RUN_HOST=0.0.0.0 \
    FLASK_RUN_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# ---------- start‑commando ----------
CMD ["sh", "-c", "gunicorn dreamz_portal:app --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout 120"]
