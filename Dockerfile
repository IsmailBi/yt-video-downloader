# Use official slim Python base image
FROM python:3.10-slim

# Install required system packages: ffmpeg, curl, build tools
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set working directory inside the container
WORKDIR /app

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy rest of your project files (e.g., app.py, .env optional, etc.)
COPY . .

# Expose the port the app will bind to (Render sets $PORT dynamically)
EXPOSE 10000

# Start the Flask app using gunicorn and dynamic $PORT
CMD gunicorn app:app --bind 0.0.0.0:${PORT} --workers 4 --log-level info
