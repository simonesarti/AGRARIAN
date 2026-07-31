FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No database driver and no PyJWT in this image, and that is the point: the portal
# reaches the database through db-writer and validates nothing itself (§7).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
