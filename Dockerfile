FROM python:3.13-slim

WORKDIR /app

# Install dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure data directory exists for sqlite volume
RUN mkdir -p /data

# Set default host and port environment variables
ENV HOST=0.0.0.0
ENV PORT=8000

# Expose the default port
EXPOSE $PORT

# Set environment variable to indicate container environment
ENV IN_DOCKER=True

CMD ["python", "main.py"]
