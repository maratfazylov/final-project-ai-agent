FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer fonts-liberation \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY research_agent ./research_agent
RUN uv sync --locked --no-dev
ENV SOFFICE_PATH=/usr/bin/soffice DATA_DIR=/app/data OUTPUT_DIR=/app/output
CMD ["uv", "run", "--locked", "--no-dev", "research-bot", "bot"]

