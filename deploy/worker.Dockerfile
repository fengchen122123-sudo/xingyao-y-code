FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && id -u 10001 >/dev/null 2>&1 || useradd --uid 10001 --create-home --shell /usr/sbin/nologin ycode

WORKDIR /opt/xingyao
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-server.txt \
    && python -m playwright install chromium

COPY . /opt/xingyao
RUN chown -R 10001:10001 /opt/xingyao

USER 10001
ENV TZ=Asia/Shanghai \
    YCODE_DATA_DIR=/data \
    YCODE_TOOL_PROFILE=server \
    YCODE_SERVE_BIND=0.0.0.0 \
    YCODE_SERVE_PORT=8766 \
    HOME=/data/home

EXPOSE 8766
CMD ["python", "gui_main.py", "--serve"]
