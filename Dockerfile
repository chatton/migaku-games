# Frame server with meikiocr (Apple Vision is macOS-only, so the container is meiki-only).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf

WORKDIR /app
COPY requirements-meiki.txt .
RUN pip install -r requirements-meiki.txt
# Bake the meikiocr models into the image so the container starts fast and works offline.
RUN python -c "from meikiocr import MeikiOCR; MeikiOCR()" && chmod -R a+rX /opt/hf
ENV HF_HUB_OFFLINE=1

COPY pipeline.py server.py ./
COPY ocr/meiki_ocr.py ocr/
COPY web/ web/

ENV MIGAKU_OCR=meiki \
    MIGAKU_HOST=0.0.0.0 \
    MIGAKU_PORT=8765 \
    MIGAKU_DATA=/data \
    MIGAKU_RETENTION_HOURS=24
VOLUME /data
EXPOSE 8765
CMD ["python", "server.py"]
