"""Cadastro dos usuarios (login e senha) que podem entrar no sistema.

O administrador continua sendo o par APP_USERNAME/APP_PASSWORD do servidor
(nunca trancado para fora, mesmo que o banco caia). Os demais usuarios sao
cadastrados pela tela "Usuarios" do proprio sistema (so o administrador abre)
e ficam guardados com a senha em HASH (pbkdf2-sha256, nunca em texto puro).

Onde ficam guardados:
    * banco Upstash Redis (REST) -- quando UPSTASH_REDIS_REST_URL/_TOKEN (ou
      KV_REST_API_URL/_TOKEN, os nomes que o Vercel injeta ao conectar o banco
      pelo painel, aba Storage) estao definidas. E o unico jeito de os usuarios
      sobreviverem no Vercel, onde o disco e apagado a cada deploy/instancia.
    * arquivo usuarios.json ao lado do app -- so quando roda no seu computador
      e nao ha banco configurado. No Vercel sem banco, o cadastro fica
      indisponivel (a tela avisa), em vez de aceitar usuarios que sumiriam.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from werkzeug.security import check_password_hash, generate_password_hash

CHAVE_REDIS = "sistema_iniciais:usuarios"
ARQUIVO_PADRAO = Path(__file__).resolve().parent / "usuarios.json"
USUARIO_RE = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")
SENHA_MIN = 8
SENHA_MAX = 128
_METODO_HASH = "pbkdf2:sha256:600000"
_TTL_EXISTE_S = 60


class UsuariosIndisponivel(Exception):
    """Sem onde guardar/ler os usuarios (banco fora do ar ou nao configurado)."""


class DadosInvalidos(ValueError):
    """Login ou senha fora das regras (mensagem pronta para mostrar na tela)."""


# -- armazenamento -----------------------------------------------------------

class _BancoRest:
    """Hash `CHAVE_REDIS` num Redis Upstash, via API REST (so httpx, sem driver)."""

    nome = "banco"

    def __init__(self, url, token):
        self._url = url
        self._token = token

    def _cmd(self, *args):
        try:
            resp = httpx.post(
                self._url,
                headers={"Authorization": f"Bearer {self._token}"},
                json=list(args),
                timeout=10.0,
            )
        except httpx.HTTPError as e:
            raise UsuariosIndisponivel(f"banco de usuários inacessível ({type(e).__name__})") from e
        if resp.status_code != 200:
            raise UsuariosIndisponivel(f"banco de usuários respondeu HTTP {resp.status_code}")
        corpo = resp.json()
        if corpo.get("error"):
            raise UsuariosIndisponivel("banco de usuários recusou o comando")
        return corpo.get("result")

    def obter(self, chave):
        valor = self._cmd("HGET", CHAVE_REDIS, chave)
        return json.loads(valor) if valor else None

    def salvar(self, chave, registro):
        self._cmd("HSET", CHAVE_REDIS, chave, json.dumps(registro, ensure_ascii=False))

    def remover(self, chave):
        return bool(self._cmd("HDEL", CHAVE_REDIS, chave))

    def todos(self):
        plano = self._cmd("HGETALL", CHAVE_REDIS) or []
        if isinstance(plano, dict):
            return {k: json.loads(v) for k, v in plano.items()}
        return {plano[i]: json.loads(plano[i + 1]) for i in range(0, len(plano) - 1, 2)}


_TRAVA_ARQUIVO = threading.Lock()


class _Arquivo:
    """Um JSON {chave: registro} ao lado do app (so uso local)."""

    nome = "arquivo"

    def __init__(self, caminho):
        self._caminho = Path(caminho)

    def _ler(self):
        try:
            return json.loads(self._caminho.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise UsuariosIndisponivel(f"não consegui ler {self._caminho.name} ({type(e).__name__})") from e

    def _gravar(self, dados):
        tmp = self._caminho.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._caminho)
        except OSError as e:
            raise UsuariosIndisponivel(f"não consegui gravar {self._caminho.name} ({type(e).__name__})") from e

    def obter(self, chave):
        with _TRAVA_ARQUIVO:
            return self._ler().get(chave)

    def salvar(self, chave, registro):
        with _TRAVA_ARQUIVO:
            dados = self._ler()
            dados[chave] = registro
            self._gravar(dados)

    def remover(self, chave):
        with _TRAVA_ARQUIVO:
            dados = self._ler()
            existia = dados.pop(chave, None) is not None
            if existia:
                self._gravar(dados)
            return existia

    def todos(self):
        with _TRAVA_ARQUIVO:
            return self._ler()


def _config_rest():
    url = (os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL") or "").strip()
    token = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN") or "").strip()
    return (url.rstrip("/"), token) if url and token else None


def _backend():
    cfg = _config_rest()
    if cfg:
        return _BancoRest(*cfg)
    if os.environ.get("VERCEL"):
        return None  # disco efemero: usuarios gravados aqui sumiriam
    return _Arquivo(ARQUIVO_PADRAO)


def nome_backend():
    """"banco", "arquivo" ou None (cadastro indisponivel neste servidor)."""
    b = _backend()
    return b.nome if b else None


def disponivel():
    return _backend() is not None


def _exigir_backend():
    b = _backend()
    if b is None:
        raise UsuariosIndisponivel(
            "Nenhum banco de usuários configurado neste servidor (veja DEPLOY.md, \"Cadastro de usuários\")."
        )
    return b


# -- regras ------------------------------------------------------------------

def chave_de(usuario):
    """Chave canonica do login: sem espacos nas pontas e sem diferenca de maiuscula."""
    return (usuario or "").strip().lower()


def validar(usuario, senha):
    usuario = (usuario or "").strip()
    if not USUARIO_RE.match(usuario):
        raise DadosInvalidos(
            "Login inválido: use de 3 a 64 caracteres entre letras, números, ponto, hífen, sublinhado ou @ (sem espaços)."
        )
    if len(senha or "") < SENHA_MIN:
        raise DadosInvalidos(f"A senha precisa ter pelo menos {SENHA_MIN} caracteres.")
    if len(senha) > SENHA_MAX:
        raise DadosInvalidos(f"A senha pode ter no máximo {SENHA_MAX} caracteres.")
    return usuario


_cache_existe = {}
_trava_cache = threading.Lock()


def _esquecer(chave):
    with _trava_cache:
        _cache_existe.pop(chave, None)


def salvar(usuario, senha):
    """Cadastra o usuario, ou troca a senha se ele ja existe. Devolve "criado" ou "atualizado"."""
    usuario = validar(usuario, senha)
    b = _exigir_backend()
    chave = chave_de(usuario)
    existia = b.obter(chave) is not None
    b.salvar(
        chave,
        {
            "usuario": usuario,
            "hash": generate_password_hash(senha, method=_METODO_HASH),
            "criado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    _esquecer(chave)
    return "atualizado" if existia else "criado"


def remover(usuario):
    b = _exigir_backend()
    chave = chave_de(usuario)
    removeu = b.remover(chave)
    _esquecer(chave)
    return removeu


def listar():
    """[{"usuario", "criado_em"}] em ordem alfabetica (nunca devolve o hash)."""
    b = _exigir_backend()
    itens = [
        {"usuario": r.get("usuario") or k, "criado_em": r.get("criado_em") or ""}
        for k, r in b.todos().items()
    ]
    return sorted(itens, key=lambda i: i["usuario"].lower())


_hash_falso = None


def verificar(usuario, senha):
    """True se login e senha batem com um usuario cadastrado. Levanta
    UsuariosIndisponivel se nao deu pra consultar o armazenamento."""
    global _hash_falso
    b = _backend()
    if b is None:
        return False
    registro = b.obter(chave_de(usuario))
    if registro is None:
        # gasta o mesmo tempo de uma verificacao real, para o tempo de
        # resposta nao revelar quais logins existem
        if _hash_falso is None:
            _hash_falso = generate_password_hash("sem-usuario", method=_METODO_HASH)
        check_password_hash(_hash_falso, senha or "")
        return False
    return check_password_hash(registro.get("hash", ""), senha or "")


def existe(usuario):
    """True se o usuario ainda esta cadastrado (resposta guardada por 60 s --
    chamado a cada request de quem entrou com um usuario cadastrado, para um
    usuario removido perder o acesso logo). Se o armazenamento nao responder,
    mantem o acesso: cair o banco nao pode derrubar quem ja estava logado."""
    chave = chave_de(usuario)
    agora = time.monotonic()
    with _trava_cache:
        guardado = _cache_existe.get(chave)
        if guardado and agora - guardado[0] < _TTL_EXISTE_S:
            return guardado[1]
    b = _backend()
    if b is None:
        return False
    try:
        achou = b.obter(chave) is not None
    except UsuariosIndisponivel:
        return True
    with _trava_cache:
        _cache_existe[chave] = (agora, achou)
    return achou
