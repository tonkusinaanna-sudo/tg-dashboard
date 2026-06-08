FROM python:3.11-slim

WORKDIR /app

# Force cache bust
ARG CACHE_BUST=3

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

EXPOSE 8000

CMD ["python", "/app/backend/main.py"]
