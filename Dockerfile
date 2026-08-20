FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Long-polling bot: no ports, no webhook, no public URL needed.
CMD ["python", "main.py"]
