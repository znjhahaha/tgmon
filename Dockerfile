FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# ffmpeg: 视频首帧兜底提取 + 可选转码（默认关闭）
# gcc/python3-dev: 少数包无 wheel 时回退编译
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata ffmpeg gcc python3-dev \
    && ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc python3-dev && apt-get autoremove -y

# Bundle the local embedding model in its own layer. Serving requests never
# downloads weights or calls an embedding API; admin and worker share this image.
COPY tgmon/embeddings.py ./tgmon/embeddings.py
RUN python -m tgmon.embeddings --download

COPY tgmon ./tgmon

RUN mkdir -p /app/db /app/sessions /app/media /app/logs

# 默认跑 admin；worker 在 compose 里覆盖 command
CMD ["python", "-m", "tgmon.admin"]
