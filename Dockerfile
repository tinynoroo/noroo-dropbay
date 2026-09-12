FROM python:3.12-alpine

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/server.py .
COPY app/static ./static

ENV DATA_DIR=/app/data
EXPOSE 8000

CMD ["python", "server.py"]
