FROM python:3.12-slim

# Dependencias de sistema do weasyprint (geracao de PDF) -- sem essas libs
# nativas (Pango/Cairo/GDK-Pixbuf) o import falha em runtime, nao em build.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    libffi8 \
    shared-mime-info \
    fonts-liberation \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Diretorios que o app espera poder escrever (uploads temporarios e as
# peticoes geradas) -- no host de producao o disco costuma ser efemero
# (some a cada deploy), o que aqui e o comportamento certo: uploads sao
# sempre apagados apos o processamento (ver app.py, finally: shutil.rmtree).
RUN mkdir -p _uploads "Peticoes Geradas"

EXPOSE 8000
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 600"]
