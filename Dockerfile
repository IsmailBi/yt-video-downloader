# Use official Python image
FROM python:3.10-slim

# Install system dependencies (FFmpeg + required tools)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Expose the port Flask will listen on (Render sets $PORT)
EXPOSE 10000

# Command to run the app with gunicorn
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:$PORT", "--workers", "4", "--log-level", "info"]
