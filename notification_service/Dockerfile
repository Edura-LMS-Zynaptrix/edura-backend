FROM python:3.12-slim

WORKDIR /app

COPY notification_service/requirements.txt .

RUN pip install --no-cache-dir \
    --retries 10 \
    --timeout 200 \
    -r requirements.txt

COPY notification_service/ .
COPY shared/ ./shared/

EXPOSE 8009

CMD ["python", "consumer.py"]