FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Needs /var/run/docker.sock mounted to start flight containers. That is a real
# privilege — see the module docstring in main.py — so this port is internal-only.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
