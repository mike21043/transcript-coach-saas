FROM python:3.11-slim

WORKDIR /app

# SSH/SCP client to push/pull files to the Vast instance
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY vast_agent.py /app/vast_agent.py

CMD ["python","/app/vast_agent.py"]
