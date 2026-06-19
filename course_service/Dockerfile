FROM python:3.12-slim


WORKDIR /app


COPY course_service/requirements.txt .


RUN pip install --no-cache-dir \
    --retries 10 \
    --timeout 200 \
    -r requirements.txt


COPY course_service/ .


COPY shared/ ./shared/


EXPOSE 8011


CMD [
    "uvicorn",
    "main:app",
    "--host",
    "0.0.0.0",
    "--port",
    "8011"
]