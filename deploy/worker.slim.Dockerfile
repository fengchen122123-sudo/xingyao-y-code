FROM python:3.12-slim

ARG PLAYWRIGHT_DOWNLOAD_HOST=""
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

USER root
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk ca-certificates libatomic1 xvfb x11vnc novnc python3-websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/xingyao
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt -r requirements-server.txt
RUN if [ -n "$PLAYWRIGHT_DOWNLOAD_HOST" ]; then \
      PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_DOWNLOAD_HOST" python -m playwright install --with-deps chromium; \
    else \
      python -m playwright install --with-deps chromium; \
    fi \
    && chmod -R a+rX /opt/ms-playwright

COPY . /opt/xingyao
RUN id -u 10001 >/dev/null 2>&1 || useradd --uid 10001 --create-home --shell /usr/sbin/nologin ycode \
    && chown -R 10001:10001 /opt/xingyao

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
CMD ["sh", "-c", "python -m gateway.worker_bootstrap && exec python gui_main.py --serve"]
