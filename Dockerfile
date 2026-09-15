FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    THCHAOS_HOST=0.0.0.0 \
    THCHAOS_PORT=8765 \
    THCHAOS_ALLOW_DEV_TOKENS=0 \
    THCHAOS_DATABASE=/var/lib/thchaos/thchaos.sqlite3

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 thchaos \
    && mkdir -p /var/lib/thchaos \
    && chown -R thchaos:thchaos /var/lib/thchaos
USER thchaos

EXPOSE 8765
VOLUME ["/var/lib/thchaos"]
CMD ["python", "-m", "thchaos_backend.server"]
