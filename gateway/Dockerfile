FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /srv/xingyao
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "gateway.app:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080"]
