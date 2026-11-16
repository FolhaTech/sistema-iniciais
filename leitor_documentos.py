"""Cliente do leitor de documentos (API `POST /ler`).

Recebe um PDF ou imagem (e, opcionalmente, uma pergunta) e devolve o texto da
`resposta` final. A API responde em NDJSON: linhas `{"status": "..."}` de
progresso (mantem o tunel vivo) e uma linha final `{"done": true, ...}`.

Configuracao (variaveis de ambiente / .env -- nunca no codigo):
    LEITOR_API_URL   endereco do servico (ex: https://xxxx.trycloudflare.com).
                     Pode vir com ou sem "/ler" no fim. O endereco de um tunel
                     trycloudflare MUDA toda vez que o tunel reinicia.
    LEITOR_API_KEY   chave enviada em "Authorization: Bearer ...".
    LEITOR_TIMEOUT_S           (opcional) segundos sem receber NENHUM dado antes
                               de desistir de uma tentativa. Padrao 180.
    LEITOR_PRAZO_TOTAL_S       (opcional) teto de uma tentativa inteira (PDF
                               escaneado roda em CPU e pode levar minutos).
                               Padrao 1200.
    LEITOR_TENTATIVAS          (opcional) tentativas em erro transitorio. Padrao 3.
    LEITOR_MAX_CONCORRENCIA    (opcional) pedidos simultaneos ao servico. Padrao 2.

Uso:
    from leitor_documentos import ler_documento
    texto = ler_documento("rg.pdf", "Qual o numero do RG?", on_status=print)
"""

import json
import os
import threading
import time
from pathlib import Path

import httpx

LIMITE_PAGINAS_SERVIDOR = 20
LIMITE_CARACTERES_SERVIDOR = 40_000

_MIME_POR_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# 429 + erros do servico/tunel (502/503/504 e os 52x proprios do Cloudflare).
_STATUS_TRANSITORIOS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class LeitorError(Exception):
    """Falha ao ler o documento (mensagem ja pronta pra mostrar ao usuario)."""


class LeitorAuthError(LeitorError):
    """HTTP 401: chave ausente ou errada."""


class LeitorIndisponivelError(LeitorError):
    """Servico/tunel fora do ar ou lento demais mesmo apos todas as tentativas."""


class _Transitorio(Exception):
    pass


def _config():
    """(url_do_endpoint, chave) -- ou levanta LeitorError se faltar algo."""
    base = (os.environ.get("LEITOR_API_URL") or "").strip().rstrip("/")
    chave = (os.environ.get("LEITOR_API_KEY") or "").strip()
    if not base or not chave:
        raise LeitorError(
            "Leitor de documentos não configurado: defina LEITOR_API_URL e "
            "LEITOR_API_KEY no .env (ou nas variáveis de ambiente do servidor)."
        )
    if not base.endswith("/ler"):
        base += "/ler"
    return base, chave


def configurado():
    """True se LEITOR_API_URL e LEITOR_API_KEY estao definidas."""
    return bool((os.environ.get("LEITOR_API_URL") or "").strip() and (os.environ.get("LEITOR_API_KEY") or "").strip())


def _num_env(nome, padrao, tipo=float):
    try:
        return tipo(os.environ.get(nome) or padrao)
    except ValueError:
        return tipo(padrao)


_semaforo = None
_semaforo_lock = threading.Lock()


def _semaforo_global():
    global _semaforo
    with _semaforo_lock:
        if _semaforo is None:
            _semaforo = threading.BoundedSemaphore(max(1, _num_env("LEITOR_MAX_CONCORRENCIA", 2, int)))
        return _semaforo


def _enviar_uma_vez(url, chave, dados, nome, mime, pergunta, on_status):
    inatividade = _num_env("LEITOR_TIMEOUT_S", 180)
    prazo_total = _num_env("LEITOR_PRAZO_TOTAL_S", 1200)
    # "read" e o tempo maximo SEM chegar nenhum byte; como o servidor manda
    # linhas de status enquanto trabalha, so estoura se o tunel/servico morreu.
    timeout = httpx.Timeout(connect=20.0, read=inatividade, write=300.0, pool=30.0)
    form = {"pergunta": pergunta} if pergunta else None
    final = None
    inicio = time.monotonic()

    try:
        with _semaforo_global():
            with httpx.Client(timeout=timeout, follow_redirects=True) as cliente:
                with cliente.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {chave}"},
                    files={"arquivo": (nome, dados, mime)},
                    data=form,
                ) as resp:
                    if resp.status_code == 401:
                        raise LeitorAuthError(
                            "Leitor de documentos recusou a chave (HTTP 401) — confira LEITOR_API_KEY."
                        )
                    if resp.status_code in _STATUS_TRANSITORIOS:
                        raise _Transitorio(f"HTTP {resp.status_code}")
                    if resp.status_code >= 400:
                        corpo = resp.read().decode("utf-8", "replace").strip()[:300]
                        raise LeitorError(f"Leitor de documentos respondeu HTTP {resp.status_code}: {corpo}")

                    for linha in resp.iter_lines():
                        linha = linha.strip()
                        if not linha:
                            continue
                        try:
                            evento = json.loads(linha)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(evento, dict):
                            continue
                        erro = evento.get("erro") or evento.get("error")
                        if erro:
                            raise LeitorError(f"Leitor de documentos: {erro}")
                        if evento.get("done"):
                            final = evento
                            break
                        if evento.get("status") and on_status:
                            on_status(str(evento["status"]))
                        if time.monotonic() - inicio > prazo_total:
                            raise LeitorIndisponivelError(
                                f"Leitor de documentos passou de {prazo_total:.0f}s lendo {nome}."
                            )
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise _Transitorio(f"{type(e).__name__}") from e

    if final is None:
        raise _Transitorio("conexão encerrada antes da linha final")
    return final


def ler_documento_completo(arquivo, pergunta=None, *, nome=None, on_status=None):
    """Envia `arquivo` (caminho ou bytes de PDF/imagem) e devolve o dict da linha
    final do NDJSON: resposta, modo, modelo, paginas_total, paginas_lidas,
    truncado, usou_visao. `on_status(texto)` recebe cada linha de progresso.

    Tenta de novo em erro de rede/timeout/5xx/429; 401 nunca e repetido."""
    url, chave = _config()

    if isinstance(arquivo, (bytes, bytearray)):
        dados = bytes(arquivo)
        nome = nome or "documento.pdf"
    else:
        caminho = Path(arquivo)
        dados = caminho.read_bytes()
        nome = nome or caminho.name
    mime = _MIME_POR_EXT.get(Path(nome).suffix.lower())
    if not mime:
        raise LeitorError(f"Tipo de arquivo não suportado pelo leitor (só PDF/PNG/JPG): {nome}")

    tentativas = max(1, _num_env("LEITOR_TENTATIVAS", 3, int))
    ultimo = ""
    for n in range(1, tentativas + 1):
        try:
            return _enviar_uma_vez(url, chave, dados, nome, mime, pergunta, on_status)
        except _Transitorio as e:
            ultimo = str(e)
            if n < tentativas:
                espera = 5 * n
                if on_status:
                    on_status(f"falha temporária ({ultimo}); tentando de novo em {espera}s ({n}/{tentativas})")
                time.sleep(espera)
    raise LeitorIndisponivelError(
        f"Leitor de documentos indisponível após {tentativas} tentativas ({ultimo}). "
        "Se o endereço é um túnel trycloudflare, ele muda quando o túnel reinicia — "
        "confira LEITOR_API_URL."
    )


def ler_documento(arquivo, pergunta=None, *, nome=None, on_status=None):
    """Como ler_documento_completo, mas devolve so o texto da `resposta`."""
    return ler_documento_completo(arquivo, pergunta, nome=nome, on_status=on_status).get("resposta") or ""
