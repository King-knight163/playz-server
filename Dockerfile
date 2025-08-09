# Use official slim python
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# system deps for pip builds and unzip
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libffi-dev \
    libssl-dev \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# copy requirements then install
COPY requirements.txt /app/
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# copy app
COPY . /app

# create run directory
RUN mkdir -p /tmp/runs

EXPOSE 8000

# Use gunicorn for production
CMD ["gunicorn", "app:app", "-w", "4", "-b", "0.0.0.0:8000", "--timeout", "120"]
