FROM python:3.12-slim

WORKDIR /app

COPY analytics_service/requirements.txt .

RUN pip install --no-cache-dir \
    --retries 10 \
    --timeout 200 \
    -r requirements.txt

COPY analytics_service/ .
COPY shared/ ./shared/

EXPOSE 8010

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]