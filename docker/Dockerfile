FROM python:3.11-slim

WORKDIR /app

# 安装依赖，通过 httpx 访问 PostgREST
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    httpx

# 安装 curl 用于健康检查
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY mem_server.py .

EXPOSE 8080

CMD ["python3", "mem_server.py"]
