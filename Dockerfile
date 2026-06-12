FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    -r requirements.txt

# Copy application code
COPY app/ .

# Configure git for commits inside container
RUN git config --global user.email "hermes@localhost" && \
    git config --global user.name "Hermes Agent"

EXPOSE 8000

CMD ["python", "main.py"]
