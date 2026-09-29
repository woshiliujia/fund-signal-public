FROM python:3.10-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        psycopg2-binary==2.9.12 Pillow==11.1.0 pytesseract==0.3.13

COPY fund_core.py server.py fund_signal.py screenshot_import.py xalpha_adapter.py ./
COPY static/ static/

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai

EXPOSE 8787

CMD ["python", "server.py", "--port", "8787"]
