FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY db_writer/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the shared DB code the sidecar needs — no ML dependencies.
# src uses namespace packages (no __init__.py files needed).
COPY src/shared/processes/constants.py src/shared/processes/constants.py
COPY src/shared/processes/db_manager.py src/shared/processes/db_manager.py
COPY db_writer/__init__.py db_writer/__init__.py
COPY db_writer/main.py db_writer/main.py

CMD ["python", "-m", "uvicorn", "db_writer.main:app", "--host", "0.0.0.0", "--port", "8000"]
