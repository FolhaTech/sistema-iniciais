FROM python:3.12-slim

# Sem dependencias de sistema pra instalar aqui -- a geracao de PDF usa
# PyMuPDF (pymupdf.Story), que nao precisa de bibliotecas nativas externas
# (ao contrario do WeasyPrint, que foi removido daqui exatamente por isso --
# ver o comentario grande em cima de render_pdf_with_footnotes no
# preencher_peticao.py).

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
