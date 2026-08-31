#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sistema web local: você escolhe a pasta do cliente pelo navegador, e o
servidor lê os documentos, chama a API da Anthropic e devolve a petição
já preenchida na tela.

Uso:
    .venv\\Scripts\\python.exe app.py
    (ou dê duplo clique em iniciar_sistema.bat)

Depois abra http://127.0.0.1:5000 no navegador.
Requer a variável de ambiente ANTHROPIC_API_KEY (veja COMO_USAR.txt).
"""
import functools
import hmac
import os
import secrets
import shutil
import uuid
from pathlib import Path

from flask import Flask, request, Response, render_template_string, abort, redirect, session, url_for
from werkzeug.utils import secure_filename

import preencher_peticao as pp
import sharepoint_client as sp

app = Flask(__name__)

# SECRET_KEY deveria vir do .env em producao (assina o cookie de sessao do
# login) -- sem ela, gera uma aleatoria a cada start, o que so teria efeito
# pratico incomodo (todo mundo deslogado a cada deploy/reinicio), nunca uma
# falha de seguranca silenciosa.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("APP_ENV") == "production"

SCRIPT_DIR = Path(__file__).resolve().parent

# Em hospedagem serverless (Vercel seta a variavel VERCEL=1 automaticamente)
# o disco onde o codigo foi implantado e SOMENTE LEITURA -- so /tmp aceita
# escrita, e e efemero (some a qualquer momento; nunca usar pra guardar
# nada que precise sobreviver ao request). Localmente (Windows, .bat)
# continua tudo dentro da propria pasta do projeto, como sempre foi.
_WRITABLE_ROOT = Path("/tmp") if os.environ.get("VERCEL") else SCRIPT_DIR
UPLOADS_ROOT = _WRITABLE_ROOT / "_uploads"
OUTPUT_DIR = _WRITABLE_ROOT / "Peticoes Geradas"
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

MODEL_OPTIONS = [
    ("claude-haiku-4-5", "Claude Haiku (padrão — mais barato e rápido)"),
    ("claude-sonnet-5", "Claude Sonnet (mais caro, pode ser mais preciso em casos difíceis)"),
]

UPLOAD_PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Preencher Petição — Reparadoras</title>
<style>
  body{font-family:Arial,Helvetica,sans-serif;background:#e7e7e7;margin:0;padding:0;}
  .navy{background:#1a2744;color:#fff;padding:18px 26px;}
  .navy h1{margin:0;font-size:18px;}
  .navy p{margin:6px 0 0 0;font-size:13px;color:#cfd6e6;}
  main{max-width:700px;margin:30px auto;background:#fff;padding:30px 36px;border-radius:6px;box-shadow:0 0 12px rgba(0,0,0,.12);}
  .erro{background:#fdecea;border:1px solid #f5988c;color:#7a1f13;padding:14px 18px;border-radius:4px;margin-bottom:20px;font-size:14px;}
  label{display:block;font-weight:bold;margin:18px 0 6px 0;font-size:14px;}
  input[type=file]{display:block;width:100%;padding:10px;border:2px dashed #999;border-radius:4px;background:#fafafa;}
  select{padding:8px;font-size:14px;width:100%;border:1px solid #ccc;border-radius:4px;}
  #lista{font-size:12.5px;color:#444;margin-top:10px;max-height:160px;overflow-y:auto;background:#f7f7f7;padding:8px 12px;border-radius:4px;}
  .origem-row{display:flex;gap:20px;margin-top:6px;}
  .origem-row label{display:flex;align-items:center;gap:6px;font-weight:normal;font-size:13.5px;margin:0;cursor:pointer;}
  .origem-row label.desabilitado{color:#999;cursor:not-allowed;}
  .origem-row input[type=radio]{margin:0;}
  button{margin-top:22px;background:#3a6ea5;color:#fff;border:none;padding:12px 22px;font-size:15px;border-radius:4px;cursor:pointer;}
  button:hover{background:#4a80bb;}
  button:disabled{background:#999;cursor:wait;}
  #status{margin-top:16px;font-size:13.5px;color:#444;display:none;}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid #ccc;border-top-color:#3a6ea5;border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:-2px;margin-right:8px;}
  @keyframes spin{to{transform:rotate(360deg);}}
</style>
</head>
<body>
<div class="navy">
  <h1>Preencher Petição Inicial — Cirurgias Reparadoras</h1>
  <p>Escolha a pasta com os documentos do cliente. O sistema lê tudo (inclusive subpastas) e preenche o modelo automaticamente.</p>
</div>
<main>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form action="/processar" method="post" enctype="multipart/form-data" id="form" onsubmit="return aoEnviar()">
    <label>Origem dos documentos</label>
    <div class="origem-row">
      <label><input type="radio" name="origem" value="upload" id="origem-upload" checked {% if erro %}disabled{% endif %}> Enviar arquivos</label>
      <label id="label-origem-sharepoint" class="{% if sharepoint_erro %}desabilitado{% endif %}" title="{{ sharepoint_erro or '' }}">
        <input type="radio" name="origem" value="sharepoint" id="origem-sharepoint" {% if erro or sharepoint_erro %}disabled{% endif %}> Selecionar pasta do SharePoint
      </label>
    </div>

    <div id="bloco-upload">
      <label for="pasta">Pasta do cliente</label>
      <input type="file" id="pasta" name="pasta" webkitdirectory directory multiple required {% if erro %}disabled{% endif %}>
      <div id="lista"></div>
    </div>

    <div id="bloco-sharepoint" style="display:none;">
      <label for="pasta_sharepoint">Pasta do cliente (SharePoint)</label>
      <input type="text" id="pasta_sharepoint" name="pasta_sharepoint" list="pastas-sharepoint-list"
             autocomplete="off" placeholder="Carregando pastas..." disabled
             {% if erro %}disabled{% endif %}>
      <datalist id="pastas-sharepoint-list"></datalist>
    </div>

    <label for="model">Modelo de IA</label>
    <select id="model" name="model" {% if erro %}disabled{% endif %}>
      {% for value, label in modelos %}
      <option value="{{ value }}">{{ label }}</option>
      {% endfor %}
    </select>

    <button type="submit" id="btn" {% if erro %}disabled{% endif %}>Processar e gerar petição</button>
    <div id="status"><span class="spinner"></span><span id="status-texto"></span></div>
  </form>
</main>
<script>
const EXT_OK = ['.pdf', '.png', '.jpg', '.jpeg', '.xlsx'];
document.getElementById('pasta').addEventListener('change', function(e){
  const files = Array.from(e.target.files).filter(f => EXT_OK.some(ext => f.name.toLowerCase().endsWith(ext)));
  const div = document.getElementById('lista');
  if (files.length === 0){
    div.innerHTML = '<em>Nenhum PDF/PNG/JPG/XLSX encontrado nessa pasta.</em>';
    return;
  }
  div.innerHTML = '<strong>' + files.length + ' documento(s) encontrado(s):</strong><br>' +
    files.map(f => f.webkitRelativePath).join('<br>');
});

function origemAtual(){
  const marcado = document.querySelector('input[name="origem"]:checked');
  return marcado ? marcado.value : 'upload';
}

function atualizarOrigem(){
  const sharepoint = origemAtual() === 'sharepoint';
  document.getElementById('bloco-upload').style.display = sharepoint ? 'none' : 'block';
  document.getElementById('bloco-sharepoint').style.display = sharepoint ? 'block' : 'none';
  document.getElementById('pasta').required = !sharepoint;
}
document.querySelectorAll('input[name="origem"]').forEach(function(r){
  r.addEventListener('change', atualizarOrigem);
});
atualizarOrigem();

let pastasSharepoint = [];
if (!document.getElementById('origem-sharepoint').disabled){
  fetch('/sharepoint/pastas').then(function(resp){
    if (!resp.ok) throw new Error('Falha ao listar pastas do SharePoint.');
    return resp.json();
  }).then(function(pastas){
    const input = document.getElementById('pasta_sharepoint');
    pastasSharepoint = pastas || [];
    if (pastasSharepoint.length === 0){
      input.placeholder = 'Nenhuma pasta de cliente encontrada';
      return;
    }
    document.getElementById('pastas-sharepoint-list').innerHTML =
      pastasSharepoint.map(function(p){ return '<option value="' + p.replace(/"/g, '&quot;') + '">'; }).join('');
    input.placeholder = 'Digite ou escolha o nome do cliente...';
    input.disabled = false;
  }).catch(function(err){
    document.getElementById('pasta_sharepoint').placeholder = 'Erro ao carregar pastas do SharePoint';
  });
}

function aoEnviar(){
  const sharepoint = origemAtual() === 'sharepoint';
  let descricao;
  if (sharepoint){
    const pasta = document.getElementById('pasta_sharepoint').value.trim();
    if (!pasta){ alert('Digite ou escolha a pasta do cliente no SharePoint.'); return false; }
    if (pastasSharepoint.length && !pastasSharepoint.includes(pasta)){
      alert('Cliente "' + pasta + '" não encontrado na lista do SharePoint. Confira o nome (escolha uma das sugestões que aparecem ao digitar).');
      return false;
    }
    descricao = 'a pasta "' + pasta + '"';
  } else {
    const files = document.getElementById('pasta').files;
    if (!files || files.length === 0){ return true; }
    descricao = files.length + ' arquivo(s)';
  }
  document.getElementById('btn').disabled = true;
  const st = document.getElementById('status');
  st.style.display = 'block';
  document.getElementById('status-texto').textContent =
    'Processando ' + descricao + '... isso pode levar alguns minutos. Não feche esta aba.';
  return true;
}
</script>
</body>
</html>
"""

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Entrar — Preencher Petição</title>
<style>
  body{font-family:Arial,Helvetica,sans-serif;background:#e7e7e7;margin:0;padding:0;}
  .navy{background:#1a2744;color:#fff;padding:18px 26px;}
  .navy h1{margin:0;font-size:18px;}
  main{max-width:360px;margin:60px auto;background:#fff;padding:30px 36px;border-radius:6px;box-shadow:0 0 12px rgba(0,0,0,.12);}
  .erro{background:#fdecea;border:1px solid #f5988c;color:#7a1f13;padding:14px 18px;border-radius:4px;margin-bottom:20px;font-size:14px;}
  label{display:block;font-weight:bold;margin:14px 0 6px 0;font-size:14px;}
  input[type=text],input[type=password]{width:100%;padding:9px;font-size:14px;border:1px solid #ccc;border-radius:4px;box-sizing:border-box;}
  button{margin-top:22px;background:#3a6ea5;color:#fff;border:none;padding:12px 22px;font-size:15px;border-radius:4px;cursor:pointer;width:100%;}
  button:hover{background:#4a80bb;}
</style>
</head>
<body>
<div class="navy"><h1>Preencher Petição Inicial</h1></div>
<main>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="post">
    <label for="usuario">Usuário</label>
    <input type="text" id="usuario" name="usuario" autofocus required>
    <label for="senha">Senha</label>
    <input type="password" id="senha" name="senha" required>
    <button type="submit">Entrar</button>
  </form>
</main>
</body>
</html>
"""


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("autenticado"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    usuario_config = os.environ.get("APP_USERNAME") or ""
    senha_config = os.environ.get("APP_PASSWORD") or ""
    erro = None
    if not usuario_config or not senha_config:
        erro = (
            "APP_USERNAME/APP_PASSWORD não estão configurados neste servidor — "
            "o login não pode funcionar até isso ser definido (veja .env.example)."
        )
        return render_template_string(LOGIN_PAGE, erro=erro)

    if request.method == "POST":
        usuario = request.form.get("usuario", "")
        senha = request.form.get("senha", "")
        if hmac.compare_digest(usuario, usuario_config) and hmac.compare_digest(senha, senha_config):
            session["autenticado"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        erro = "Usuário ou senha incorretos."

    return render_template_string(LOGIN_PAGE, erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def api_key_error():
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return (
            "A variável ANTHROPIC_API_KEY não está configurada nesta máquina. "
            "Veja o arquivo COMO_USAR.txt para o passo a passo (é rápido, leva 2 minutos)."
        )
    return None


@app.route("/")
@login_required
def index():
    return render_template_string(
        UPLOAD_PAGE, erro=api_key_error(), sharepoint_erro=sp.config_error(), modelos=MODEL_OPTIONS
    )


@app.route("/sharepoint/pastas")
@login_required
def sharepoint_pastas():
    erro = sp.config_error()
    if erro:
        return {"erro": erro}, 400
    try:
        return sp.list_client_folders()
    except sp.SharePointError as e:
        return {"erro": str(e)}, 502


def _erro_pagina(mensagem):
    return render_template_string(
        UPLOAD_PAGE, erro=mensagem, sharepoint_erro=sp.config_error(), modelos=MODEL_OPTIONS
    )


@app.route("/processar", methods=["POST"])
@login_required
def processar():
    erro = api_key_error()
    if erro:
        return _erro_pagina(erro)

    model = request.form.get("model", pp.DEFAULT_MODEL)
    origem = request.form.get("origem", "upload")

    session_id = uuid.uuid4().hex[:8]
    session_dir = UPLOADS_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    client_folder_name = "cliente"
    try:
        if origem == "sharepoint":
            pasta = (request.form.get("pasta_sharepoint") or "").strip()
            if not pasta:
                return _erro_pagina("Selecione a pasta do cliente no SharePoint.")
            client_folder_name = pasta
            sp.download_client_folder(pasta, session_dir)
        else:
            uploaded = [f for f in request.files.getlist("pasta") if f.filename]
            if not uploaded:
                return _erro_pagina("Nenhum arquivo recebido — selecione a pasta do cliente.")

            saved_files = []
            for f in uploaded:
                rel_path = f.filename.replace("\\", "/")
                parts = [secure_filename(p) or "arquivo" for p in rel_path.split("/") if p]
                if not parts:
                    continue
                if len(rel_path.split("/")) > 0 and "/" in rel_path:
                    client_folder_name = rel_path.split("/")[0] or client_folder_name
                dest = session_dir.joinpath(*parts)
                if dest.suffix.lower() not in pp.SUPPORTED_EXT and dest.suffix.lower() not in pp.UPLOAD_EXTRA_EXT:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                f.save(dest)
                saved_files.append(dest)

            if not saved_files:
                return _erro_pagina("Nenhum PDF/PNG/JPG/XLSX encontrado nos arquivos enviados.")

        files = pp.find_documents(session_dir)
        if not files:
            return _erro_pagina("Nenhum PDF/PNG/JPG/XLSX encontrado na pasta do cliente.")
        batches = pp.make_batches(files)

        import anthropic
        client = anthropic.Anthropic()

        all_results = []
        for i, batch in enumerate(batches, 1):
            all_results.append(pp.call_batch(client, model, batch, str(i)))
            if pp.eh_lote_critico(batch):
                all_results.append(pp.call_batch(client, model, batch, f"{i}b"))

        data, _ = pp.merge_results(all_results)

        nome_cliente = data.get("cliente_nome_completo") or client_folder_name
        output_path = OUTPUT_DIR / f"Petição Inicial - {nome_cliente}.html"
        output_path = pp.unique_output_path(output_path)

        pp.fill_template(pp.DEFAULT_TEMPLATE, output_path, data, files=files)

        html_content = output_path.read_text(encoding="utf-8")
        return Response(html_content, mimetype="text/html")

    except Exception as e:
        return _erro_pagina(f"Erro ao processar: {e}")
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)


@app.route("/gerar-pdf", methods=["POST"])
@login_required
def gerar_pdf():
    html_content = request.get_data(as_text=True)
    if not html_content or "<html" not in html_content.lower():
        abort(400, "HTML da petição não recebido.")
    try:
        pdf_bytes = pp.render_pdf_with_footnotes(html_content)
    except Exception as e:
        abort(500, f"Erro ao gerar PDF: {e}")
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="peticao.pdf"'},
    )


if __name__ == "__main__":
    print("Abra no navegador: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
