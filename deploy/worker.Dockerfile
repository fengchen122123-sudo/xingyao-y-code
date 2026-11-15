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
    HOME=/data/home \
    YCODE_KEYRING_FILE=/run/keyring/crypted_pass.cfg \
    YCODE_KEYRING_PASSWORD_FILE=/run/secrets/keyring_password \
    YCODE_KEYRING_READ_ONLY=1

EXPOSE 8766
# 先解锁共享钥匙串（只读挂载网关的 crypted_pass.cfg），失败就别起服务。
CMD ["sh", "-c", "python -m gateway.worker_bootstrap && exec python gui_main.py --serve"]
