FROM python:3.14-alpine

WORKDIR /app
COPY notifier.py /app/notifier.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/notifier.py"]
