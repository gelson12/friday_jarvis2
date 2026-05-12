# Multi-stage build for Friday_jarvis2 LiveKit Agent
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install system dependencies required for audio and LiveKit
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libopus-dev \
    libopus0 \
    libvorbis-dev \
    libvorbisenc2 \
    libvorbisfile3 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements.txt

# Copy application code
COPY agent.py .
COPY prompts.py .
COPY tools.py .
COPY mcp_client/ ./mcp_client/
COPY thirdparty/ ./thirdparty/

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run the LiveKit agent
CMD ["python", "-m", "livekit.agents", "agent.entrypoint"]
