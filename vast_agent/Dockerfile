FROM python:3.11-slim

WORKDIR /app
COPY .. /app

RUN pip install --no-cache-dir requests redis

# Run the packaged controller module
CMD ["python", "-m", "vast_agent.vast_agent"]
