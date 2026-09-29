FROM python:3.10-slim

WORKDIR /app

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple psycopg2-binary==2.9.12

COPY fund_core.py server.py fund_signal.py ./
COPY static/ static/

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai

EXPOSE 8787

CMD ["python", "server.py", "--port", "8787"]
