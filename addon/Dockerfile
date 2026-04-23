FROM python:3.12-alpine

ENV LANG=C.UTF-8

RUN pip3 install --no-cache-dir aiohttp

COPY app/ /app/

CMD ["python3", "/app/main.py"]
