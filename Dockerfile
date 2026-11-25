FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY bridge ./bridge

EXPOSE 5000 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import os,urllib.request; port=os.environ.get('HTTP_PORT','8090'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)"

CMD ["python3", "-m", "bridge.main"]
