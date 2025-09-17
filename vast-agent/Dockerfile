FROM python:3.11-slim

WORKDIR /app
COPY vast_agent.py .

RUN pip install --no-cache-dir requests redis

CMD ["python", "vast_agent.py"]
