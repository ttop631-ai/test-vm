FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_PORT=9000 \
    AGENT_KEEPALIVE_SEC=30

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --system --no-create-home --uid 10001 agent
USER agent

EXPOSE 9000

# 단일 워커. access log는 main.py 미들웨어가 key=value 형식으로 남긴다.
# keep-alive는 console의 HTTP_KEEPALIVE_EXPIRY(15s)보다 길어야 한다 (SPEC §5, 경합 방지).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${AGENT_PORT} --timeout-keep-alive ${AGENT_KEEPALIVE_SEC} --no-access-log"]
