FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LISTEN_PORT=8000

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nodes.json .
COPY app ./app

RUN useradd --system --no-create-home --uid 10001 console \
 && mkdir -p /data && chown console /data
USER console

EXPOSE 8000

# 단일 워커 필수: 노드 상태와 백그라운드 태스크 레지스트리가 프로세스 메모리에 있다.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${LISTEN_PORT} --no-access-log"]
