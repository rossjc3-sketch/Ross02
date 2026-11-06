FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONUNBUFFERED=1 HF_HOME=/data/models QUICKCUT_DATA=/data
COPY app.py engine.py autocut.py cloud.py ./
COPY web ./web
CMD ["python", "cloud.py"]
