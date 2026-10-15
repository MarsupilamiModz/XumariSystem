# XumariSystem – shared image for the bot and the API service
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# fonts for PDF invoices / captcha images
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY api ./api
COPY migrations ./migrations
COPY legal ./legal

RUN useradd --create-home --uid 10001 xumari \
    && mkdir -p /app/data /app/logs \
    && chown -R xumari:xumari /app
USER xumari

EXPOSE 8080
CMD ["python", "-m", "bot.main"]
