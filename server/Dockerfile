FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    websockets==12.0 \
    aiohttp==3.9.5 \
    aiosqlite==0.20.0

COPY server.py .

CMD ["python3", "-u", "server.py"]
