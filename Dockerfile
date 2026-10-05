# Frame server with meikiocr (Apple Vision is macOS-only, so the container is meiki-only).
# Published by CI to ghcr.io/chatton/migaku-games (linux/amd64, linux/arm64).
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
# JMdict (CC BY-SA, EDRDG) as a lookup table for coloured translations (align.py).
COPY tools/build_jmdict.py tools/
RUN python tools/build_jmdict.py /opt/jmdict.sqlite

COPY pipeline.py server.py config.py align.py ./
# The default config; compose mounts ./config over it.
COPY config/config.yaml /config/config.yaml
COPY ocr/meiki_ocr.py ocr/
COPY web/ web/

ENV MIGAKU_OCR=meiki \
    MIGAKU_HOST=0.0.0.0 \
    MIGAKU_PORT=8765 \
    MIGAKU_DATA=/data \
    MIGAKU_CONFIG=/config/config.yaml \
    MIGAKU_JMDICT=/opt/jmdict.sqlite
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/config', timeout=4)"
CMD ["python", "server.py"]
