# 默认走 Docker Hub；国内服务器拉不到镜像时，在 .env 里加
# DOCKER_REGISTRY=docker.1ms.run 由 compose 传进来即可（见 README 故障排查）。
# 只写域名，不要带 https://（那是 daemon.json 的 registry-mirrors 的写法，
# 写进 FROM 会报 invalid reference format）。
# 注意带域名时官方镜像必须补全 library/ 命名空间。
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    THCHAOS_HOST=0.0.0.0 \
    THCHAOS_PORT=8765 \
    THCHAOS_ALLOW_DEV_TOKENS=0 \
    THCHAOS_DATABASE=/var/lib/thchaos/thchaos.sqlite3

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# 同理，PyPI 也常在国内容易超时；未设 PIP_INDEX_URL 时展开为空，行为不变。
ARG PIP_INDEX_URL=
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} .

RUN useradd --create-home --uid 10001 thchaos \
    && mkdir -p /var/lib/thchaos \
    && chown -R thchaos:thchaos /var/lib/thchaos
USER thchaos

EXPOSE 8765
VOLUME ["/var/lib/thchaos"]
CMD ["python", "-m", "thchaos_backend.server"]
