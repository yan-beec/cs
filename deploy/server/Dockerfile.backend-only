FROM python:3.10.21-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN mkdir -p /app/log

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ics_agent ./ics_agent
COPY knowledge_base_in ./knowledge_base_in

EXPOSE 3001

CMD ["python", "-m", "ics_agent.app.serve"]
