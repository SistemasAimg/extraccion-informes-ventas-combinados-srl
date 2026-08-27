# Dockerfile
FROM python:3.10-slim

# Ajustes de Python/pip
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Certificados para HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias primero (mejor caching)
COPY requirements.txt .
RUN python -m pip install --upgrade pip && pip install -r requirements.txt

# Copiar el resto del proyecto
COPY . .

# Por defecto usamos este vars.yml dentro del contenedor
ENV VARS_FILE=/app/vars.yml
# Podés setear flags extra (ej: "--debug" o "--insecure") desde el Job
ENV EXTRA_FLAGS=""

# Usuario no root
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Ejecuta el runner con el vars.yml indicado por env
# (soporta override: ENV VARS_FILE=/app/otro.yml)
CMD ["sh", "-c", "python caddis_combined_job.py --vars ${VARS_FILE:-/app/vars.yml} ${EXTRA_FLAGS}"]