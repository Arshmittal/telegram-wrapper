FROM python:3.9-slim

WORKDIR /app

# List files to debug
RUN ls -la

# Copy all files from current directory
COPY . .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Set environment variables
ENV PORT=8000

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]