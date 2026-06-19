FROM python:3.12-slim

WORKDIR /app

COPY progress_service/requirements.txt .

RUN pip install --no-cache-dir \
    --retries 10 \
    --timeout 200 \
    -r requirements.txt

COPY progress_service/ .
COPY shared/ ./shared/

EXPOSE 8008

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8008"]