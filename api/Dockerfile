FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /srv

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /srv/requirements.txt
RUN pip install -r /srv/requirements.txt

# Source is bind-mounted at /srv/app in dev; this COPY keeps the image runnable alone.
COPY . /srv

ENV PYTHONPATH=/srv

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
