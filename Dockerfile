FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

VOLUME /app/data
ENV OW_DB_PATH=/app/data/ow_stats.db

ENTRYPOINT ["python3", "app.py"]
