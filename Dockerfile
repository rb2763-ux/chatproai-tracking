FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY main_redis.py .

# Run
EXPOSE 8000
CMD ["uvicorn", "main_redis:app", "--host", "0.0.0.0", "--port", "8000"]
