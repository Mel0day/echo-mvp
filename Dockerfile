FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy source
COPY echo/ ./echo/
COPY static/ ./static/

# Data directory for LanceDB persistence
RUN mkdir -p /data

ENV ECHO_DATA_DIR=/data
ENV PORT=8000

EXPOSE 8000

COPY start.sh ./
RUN chmod +x start.sh
CMD ["./start.sh"]
