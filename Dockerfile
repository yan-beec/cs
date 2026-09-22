FROM python:3.10.21-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ics_agent ./ics_agent
COPY knowledge_base_in ./knowledge_base_in

EXPOSE 3001

CMD ["sh", "-c", "uvicorn ics_agent.app.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-3001}"]
