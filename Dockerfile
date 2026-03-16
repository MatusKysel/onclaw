FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

VOLUME /data
ENV ONCLAW_MEMORY_PATH=/data/onclaw_memory.db

ENTRYPOINT ["python", "-m", "onclaw"]
