FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md ./
COPY indexhook ./indexhook
RUN pip install --no-cache-dir .
RUN useradd --system --home /data --create-home indexhook
USER indexhook
ENV INDEXHOOK_DATA_DIR=/data INDEXHOOK_HOST=0.0.0.0 INDEXHOOK_PORT=8787
VOLUME /data
EXPOSE 8787
CMD ["indexhook", "serve"]
