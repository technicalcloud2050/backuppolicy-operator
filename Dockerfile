FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backup_controller/ ./backup_controller/
USER 10001
CMD ["kopf", "run", "--standalone", "--verbose", "/app/backup_controller/main.py"]
