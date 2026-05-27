FROM python:3.10-slim

# Use Aliyun Debian mirror
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true

# Minimal system dependencies: curl AND git
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (fixed UID/GID=1000)
RUN groupadd -g 1000 autogpt && useradd -u 1000 -g autogpt -m -s /bin/bash autogpt

# Install Python dependencies
WORKDIR /app
COPY requirements-minimal.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    --index-url https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com

# Copy source code
COPY source/ /app/

# Remove sensitive files
RUN rm -f /app/.env /app/.env.* /app/.envrc

# Set permissions
RUN chown -R autogpt:autogpt /app

# Switch to non-root user
USER autogpt

ENV PYTHONPATH=/app
ENV HOME=/home/autogpt

ENTRYPOINT ["python", "-m", "autogpt"]
