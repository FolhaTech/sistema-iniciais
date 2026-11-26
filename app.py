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
import datetime
import functools
import hmac
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path

from flask import Flask, request, Response, render_template_string, abort, redirect, session, url_for
from werkzeug.utils import secure_filename

import preencher_peticao as pp
import sharepoint_client as sp
import agente_fatos
import ui_tema
import usuarios

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
<title>Preencher Petição — Arantes Arimura</title>
<!--HEAD-->
<style>
  input[type=file]{display:block;width:100%;padding:12px;border:2px dashed #b9b6b6;border-radius:6px;background:#fafafa;font:inherit;font-size:13.5px;}
  #lista{font-size:12.5px;color:#444;margin-top:10px;max-height:160px;overflow-y:auto;background:#f6f5f5;padding:8px 12px;border-radius:6px;}
  #lista:empty{display:none;}
  .origem-row{display:flex;gap:22px;margin-top:6px;flex-wrap:wrap;}
  .origem-row label{display:flex;align-items:center;gap:6px;font-weight:400;font-size:13.5px;margin:0;cursor:pointer;color:var(--texto);}
  .origem-row label.desabilitado{color:#999;cursor:not-allowed;}
  .origem-row input[type=radio]{margin:0;accent-color:var(--vermelho);}
  #status{margin-top:18px;font-size:13.5px;color:#444;display:none;}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid #ccc;border-top-color:var(--vermelho);border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:-2px;margin-right:8px;}
  @keyframes spin{to{transform:rotate(360deg);}}
</style>
</head>
<body>
<!--TOPO-->
<section class="hero">
  <h1>Preencher Petição Inicial</h1>
  <p>Cirurgias reparadoras: escolha a pasta com os documentos do cliente. O sistema lê tudo (inclusive subpastas) e preenche o modelo automaticamente.</p>
</section>
<main class="painel">
<div class="cartao moldura">
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

    <button type="submit" class="btn" id="btn" {% if erro %}disabled{% endif %}>Processar e gerar petição</button>
    <div id="status"><span class="spinner"></span><span id="status-texto"></span></div>
  </form>
</div>
</main>
<script>
const EXT_OK = ['.pdf', '.png', '.jpg', '.jpeg', '.xlsx', '.docx'];
document.getElementById('pasta').addEventListener('change', function(e){
  const files = Array.from(e.target.files).filter(f => EXT_OK.some(ext => f.name.toLowerCase().endsWith(ext)));
  const div = document.getElementById('lista');
  if (files.length === 0){
    div.innerHTML = '<em>Nenhum PDF/PNG/JPG/XLSX/DOCX encontrado nessa pasta.</em>';
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
<title>Entrar — Arantes Arimura</title>
<!--HEAD-->
<style>.painel{max-width:440px;}</style>
</head>
<body>
<!--TOPO-->
<section class="hero">
  <h1>Preencher Petição Inicial</h1>
  <p>Acesso restrito à equipe do escritório.</p>
</section>
<main class="painel">
<div class="cartao moldura">
  <h2 class="titulo">Entrar</h2>
  <p class="sub">Informe seu login e senha.</p>
  {% if erro %}<div class="erro" style="margin-top:16px;">{{ erro }}</div>{% endif %}
  <form method="post">
    <label for="usuario">Login</label>
    <input type="text" id="usuario" name="usuario" autofocus required autocomplete="username">
    <label for="senha">Senha</label>
    <input type="password" id="senha" name="senha" required autocomplete="current-password">
    <button type="submit" class="btn btn-azul btn-bloco">Entrar</button>
  </form>
</div>
</main>
</body>
</html>
"""


USUARIOS_PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Usuários — Arantes Arimura</title>
<!--HEAD-->
</head>
<body>
<!--TOPO-->
<section class="hero">
  <h1>Usuários do sistema</h1>
  <p>Cadastre quem pode entrar com login e senha. Só o administrador vê esta tela.</p>
</section>
<main class="painel">
  {% if msg %}<div class="ok">{{ msg }}</div>{% endif %}
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}

  <div class="cartao moldura">
    <h2 class="titulo">Cadastrar usuário</h2>
    {% if not armazenamento %}
      <div class="aviso" style="margin-top:16px;">
        Este servidor não tem onde guardar os usuários: no Vercel o disco é apagado a cada deploy.
        Crie um banco <strong>Upstash Redis</strong> no painel do Vercel (aba Storage) e conecte ao projeto;
        as variáveis são criadas sozinhas. Depois faça um novo deploy. Passo a passo em DEPLOY.md.
      </div>
    {% else %}
      <p class="sub">
        {% if armazenamento == 'banco' %}Guardado no banco (Upstash Redis).
        {% else %}Guardado no arquivo <code>usuarios.json</code>, só neste computador.{% endif %}
        Se o login já existe, a senha dele é trocada.
      </p>
      <form method="post" autocomplete="off">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="acao" value="salvar">
        <label for="usuario">Login</label>
        <input type="text" id="usuario" name="usuario" required minlength="3" maxlength="64" autocomplete="off">
        <label for="senha">Senha</label>
        <input type="password" id="senha" name="senha" required minlength="8" maxlength="128" autocomplete="new-password">
        <label for="confirmacao">Repetir a senha</label>
        <input type="password" id="confirmacao" name="confirmacao" required minlength="8" maxlength="128" autocomplete="new-password">
        <p class="dica">Login: 3 a 64 caracteres (letras, números, ponto, hífen, sublinhado ou @). Senha: mínimo 8 caracteres.</p>
        <button type="submit" class="btn btn-azul">Salvar usuário</button>
      </form>
    {% endif %}
  </div>

  <div class="cartao">
    <h2 class="titulo">Usuários cadastrados</h2>
    <table class="lista">
      <thead><tr><th>Login</th><th>Cadastrado em</th><th></th></tr></thead>
      <tbody>
        <tr>
          <td>{{ admin_nome }}<span class="etiqueta">administrador</span></td>
          <td>definido no servidor</td>
          <td class="acao"></td>
        </tr>
        {% for u in lista %}
        <tr>
          <td>{{ u.usuario }}</td>
          <td>{{ u.criado_em }}</td>
          <td class="acao">
            <form method="post" onsubmit="return confirm('Remover este usuário? Ele perde o acesso na hora.');" style="display:inline;">
              <input type="hidden" name="csrf" value="{{ csrf }}">
              <input type="hidden" name="acao" value="remover">
              <input type="hidden" name="usuario" value="{{ u.usuario }}">
              <button type="submit" class="btn btn-link">Remover</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% if not lista %}<p class="dica">Nenhum outro usuário cadastrado.</p>{% endif %}
  </div>
</main>
</body>
</html>
"""

UPLOAD_PAGE = ui_tema.aplicar(UPLOAD_PAGE)
LOGIN_PAGE = ui_tema.aplicar(LOGIN_PAGE)
USUARIOS_PAGE = ui_tema.aplicar(USUARIOS_PAGE)


def _iguais(a, b):
    """Comparacao em tempo constante (bytes: compare_digest recusa str com acento)."""
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _admin_config():
    return os.environ.get("APP_USERNAME") or "", os.environ.get("APP_PASSWORD") or ""


def _eh_admin():
    # Sessao criada antes do cadastro de usuarios nao tem "usuario": so o
    # administrador (APP_USERNAME) existia, entao ela vale como admin.
    return bool(session.get("autenticado")) and (session.get("admin") is True or "usuario" not in session)


@app.context_processor
def _contexto_tema():
    if not session.get("autenticado"):
        return {"usuario_logado": "", "eh_admin": False}
    return {
        "usuario_logado": session.get("usuario") or _admin_config()[0],
        "eh_admin": _eh_admin(),
    }


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("autenticado"):
            # So volta pra rota original se for GET: rotas como /processar so
            # aceitam POST, e um redirect pos-login e sempre GET (dava 405).
            proxima = request.path if request.method == "GET" else None
            return redirect(url_for("login", next=proxima))
        usuario = session.get("usuario")
        if usuario and not session.get("admin") and not usuarios.existe(usuario):
            session.clear()  # usuario removido na tela "Usuarios"
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @login_required
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not _eh_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _destino_seguro(destino):
    """So aceita caminho do proprio site (evita redirecionar para fora apos o login)."""
    if destino and destino.startswith("/") and not destino.startswith("//") and "\\" not in destino:
        return destino
    return url_for("index")


@app.route("/login", methods=["GET", "POST"])
def login():
    admin_nome, admin_senha = _admin_config()
    if not admin_nome or not admin_senha:
        erro = (
            "APP_USERNAME/APP_PASSWORD não estão configurados neste servidor — "
            "o login não pode funcionar até isso ser definido (veja .env.example)."
        )
        return render_template_string(LOGIN_PAGE, erro=erro)

    erro = None
    if request.method == "POST":
        usuario = (request.form.get("usuario") or "").strip()
        senha = request.form.get("senha") or ""
        entrou = None  # (nome, eh_admin)
        if _iguais(usuario, admin_nome) and _iguais(senha, admin_senha):
            entrou = (admin_nome, True)
        else:
            try:
                if usuarios.verificar(usuario, senha):
                    entrou = (usuario, False)
            except usuarios.UsuariosIndisponivel as e:
                print(f"[aviso] cadastro de usuários indisponível no login: {e}")
                erro = "Não consegui consultar o cadastro de usuários agora. Tente de novo em instantes."
        if entrou:
            session.clear()  # sessao nova a cada login
            session["autenticado"] = True
            session["usuario"], session["admin"] = entrou
            session.permanent = True
            return redirect(_destino_seguro(request.args.get("next")))
        erro = erro or "Usuário ou senha incorretos."
        time.sleep(0.5)  # freia tentativa de adivinhar a senha

    return render_template_string(LOGIN_PAGE, erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _csrf_token():
    if not session.get("csrf"):
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def _csrf_ok():
    esperado = session.get("csrf") or ""
    return bool(esperado) and _iguais(request.form.get("csrf") or "", esperado)


_FUSO_BRASIL = datetime.timezone(datetime.timedelta(hours=-3))


def _data_br(iso):
    try:
        return datetime.datetime.fromisoformat(iso).astimezone(_FUSO_BRASIL).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return iso or ""


@app.route("/usuarios", methods=["GET", "POST"])
@admin_required
def usuarios_page():
    if request.method == "POST":
        if not _csrf_ok():
            abort(400)
        aviso = {}
        acao = request.form.get("acao")
        try:
            if acao == "salvar":
                login_novo = (request.form.get("usuario") or "").strip()
                senha = request.form.get("senha") or ""
                if senha != (request.form.get("confirmacao") or ""):
                    raise usuarios.DadosInvalidos("A confirmação da senha não confere.")
                if _iguais(login_novo.lower(), _admin_config()[0].lower()):
                    raise usuarios.DadosInvalidos(
                        "Esse login é o do administrador (definido no servidor). Escolha outro."
                    )
                resultado = usuarios.salvar(login_novo, senha)
                aviso["msg"] = f"Usuário {login_novo} {resultado}."
            elif acao == "remover":
                alvo = (request.form.get("usuario") or "").strip()
                if usuarios.remover(alvo):
                    aviso["msg"] = f"Usuário {alvo} removido."
                else:
                    aviso["erro"] = "Esse usuário não existe mais."
            else:
                abort(400)
        except (usuarios.DadosInvalidos, usuarios.UsuariosIndisponivel) as e:
            aviso["erro"] = str(e)
        session["aviso_usuarios"] = aviso
        return redirect(url_for("usuarios_page"))

    aviso = session.pop("aviso_usuarios", None) or {}
    lista, erro = [], aviso.get("erro")
    if usuarios.disponivel():
        try:
            lista = [
                {"usuario": u["usuario"], "criado_em": _data_br(u["criado_em"])} for u in usuarios.listar()
            ]
        except usuarios.UsuariosIndisponivel as e:
            erro = erro or str(e)
    return render_template_string(
        USUARIOS_PAGE,
        msg=aviso.get("msg"),
        erro=erro,
        csrf=_csrf_token(),
        armazenamento=usuarios.nome_backend(),
        lista=lista,
        admin_nome=_admin_config()[0],
    )


def api_key_error():
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return (
            "A variável GEMINI_API_KEY não está configurada neste servidor. "
            "Defina-a no arquivo .env (local) ou nas Environment Variables do Vercel."
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
                return _erro_pagina("Nenhum PDF/PNG/JPG/XLSX/DOCX encontrado nos arquivos enviados.")

        files = pp.find_documents(session_dir)
        if not files:
            return _erro_pagina("Nenhum PDF/PNG/JPG/XLSX/DOCX encontrado na pasta do cliente.")
        batches = pp.make_batches(files)

        client = pp.make_client()

        all_results = pp.processar_lotes(client, model, batches)

        data, _ = pp.merge_results(all_results)
        pp.completar_dados_operadora(client, model, files, data)
        agente_fatos.aplicar_agente_fatos(client, model, files, data)

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


@app.route("/gerar-docx", methods=["POST"])
@login_required
def gerar_docx():
    html_content = request.get_data(as_text=True)
    if not html_content or "<html" not in html_content.lower():
        abort(400, "HTML da petição não recebido.")
    try:
        docx_bytes = pp.render_docx(html_content)
    except Exception as e:
        abort(500, f"Erro ao gerar Word: {e}")
    return Response(
        docx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="peticao.docx"'},
    )


if __name__ == "__main__":
    print("Abra no navegador: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
