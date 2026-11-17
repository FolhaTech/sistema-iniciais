"""Adaptador do Gemini (Google) com a mesma "cara" do SDK da Anthropic.

O pipeline do sistema (preencher_peticao.py) chama
`client.messages.create(model=, max_tokens=, system=, messages=, output_config=)`
e le `response.content[i].type/.text`. Este modulo traduz essa chamada para
`POST /v1beta/models/{modelo}:generateContent` e devolve um objeto no mesmo
formato -- assim lotes, PDF->markdown, dupla consulta, retry e fallbacks de
JSON continuam funcionando sem nenhuma mudanca. Erros HTTP viram as MESMAS
excecoes da biblioteca `anthropic` (BadRequestError, RateLimitError,
InternalServerError, APIConnectionError) para o tratamento existente valer.

Configuracao (.env / variaveis de ambiente -- nunca no codigo):
    GEMINI_API_KEY             chave do Google AI Studio (aistudio.google.com/apikey)
    GEMINI_MODEL               (opcional) um ou mais IDs separados por virgula; se o
                               primeiro nao existir/estiver sem cota, tenta o proximo.
    GEMINI_MAX_CONCORRENCIA    (opcional) chamadas simultaneas (padrao 2 -- o plano
                               gratis tem poucas requisicoes por minuto).
    GEMINI_MAX_OUTPUT_TOKENS   (opcional) teto de saida (padrao 32768; modelos com
                               "raciocinio" gastam parte disso antes de responder).

ATENCAO (LGPD): no plano GRATIS o Google pode usar o conteudo enviado para
melhorar produtos e revisores humanos podem le-lo. So o plano pago (faturamento
ativado no projeto) fica de fora disso.
"""

import os
import re
import threading
import time
from types import SimpleNamespace

import httpx

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MODELOS_PADRAO = "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"


def _chave():
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def configurado():
    return bool(_chave())


def _num_env(nome, padrao):
    try:
        return int(os.environ.get(nome) or padrao)
    except ValueError:
        return int(padrao)


class _ModeloIndisponivel(Exception):
    pass


def _mensagem_erro(resp):
    try:
        return str((resp.json().get("error") or {}).get("message") or resp.text)[:500]
    except Exception:
        return (resp.text or "")[:500]


def _retry_delay(resp):
    """Segundos pedidos pelo Google em RetryInfo.retryDelay ("33s"), ou None."""
    try:
        for d in (resp.json().get("error") or {}).get("details") or []:
            m = re.fullmatch(r"([\d.]+)s", str(d.get("retryDelay") or ""))
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None


def _blocos_para_partes(content):
    """Blocos no formato da Anthropic -> `parts` do Gemini."""
    if isinstance(content, str):
        return [{"text": content}]
    partes = []
    for b in content:
        tipo = b.get("type")
        if tipo == "text":
            partes.append({"text": b["text"]})
        elif tipo in ("document", "image"):
            src = b["source"]
            partes.append({"inlineData": {"mimeType": src["media_type"], "data": src["data"]}})
    return partes


def _texto_system(system):
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b["text"] for b in system if b.get("type") == "text")


class _Messages:
    def __init__(self, cliente):
        self._c = cliente

    def create(self, *, model=None, max_tokens=None, system=None, messages=None, output_config=None, **_ignorado):
        return self._c._gerar(system, messages or [], output_config)


class GeminiClient:
    """Drop-in minimo de `anthropic.Anthropic()` (so `.messages.create`)."""

    def __init__(self):
        if not configurado():
            raise RuntimeError("GEMINI_API_KEY não está configurada (.env ou variável de ambiente).")
        modelos = os.environ.get("GEMINI_MODEL") or MODELOS_PADRAO
        self._modelos = [m.strip() for m in modelos.split(",") if m.strip()]
        self._indice = 0  # modelo que funcionou por ultimo (compartilhado entre threads)
        self._trava = threading.Lock()
        self._sem = threading.BoundedSemaphore(max(1, _num_env("GEMINI_MAX_CONCORRENCIA", 2)))
        self.messages = _Messages(self)

    # -- monta o corpo e percorre os modelos candidatos ----------------------
    def _gerar(self, system, messages, output_config):
        corpo = {
            "contents": [
                {"role": "model" if m["role"] == "assistant" else "user", "parts": _blocos_para_partes(m["content"])}
                for m in messages
            ],
            "generationConfig": {"maxOutputTokens": _num_env("GEMINI_MAX_OUTPUT_TOKENS", 32768)},
        }
        instrucao = _texto_system(system)
        if instrucao:
            corpo["systemInstruction"] = {"parts": [{"text": instrucao}]}
        schema = ((output_config or {}).get("format") or {}).get("schema")
        if schema:
            corpo["generationConfig"]["responseMimeType"] = "application/json"
            corpo["generationConfig"]["responseJsonSchema"] = schema

        import anthropic

        # Percorre os modelos a partir do primeiro ainda valido. Modelo que NAO
        # EXISTE / esta sem cota e descartado de vez (_indice avanca); modelo
        # apenas SOBRECARREGADO (503 "high demand", 429) so e pulado nesta
        # chamada -- na proxima o principal volta a ser tentado primeiro. So
        # levanta erro quando TODOS falharam, e o ultimo erro de sobrecarga e o
        # que sobe (o call_batch ainda tem o seu proprio retry em cima).
        ultimo_erro = None
        with self._trava:
            primeiro = self._indice
        for indice in range(primeiro, len(self._modelos)):
            modelo = self._modelos[indice]
            try:
                return self._chamar(modelo, corpo)
            except _ModeloIndisponivel as e:
                print(f"     [aviso] modelo Gemini {modelo} indisponível ({e}); tentando o próximo.")
                with self._trava:
                    if self._indice == indice:
                        self._indice = indice + 1
            except (anthropic.InternalServerError, anthropic.RateLimitError, anthropic.APIConnectionError) as e:
                # APIConnectionError inclui APITimeoutError (modelo travado)
                ultimo_erro = e
                if indice + 1 < len(self._modelos):
                    print(f"     [aviso] modelo Gemini {modelo} sobrecarregado/sem resposta; tentando {self._modelos[indice + 1]}.")
        if ultimo_erro is not None:
            raise ultimo_erro
        raise anthropic.NotFoundError(
            f"Nenhum modelo Gemini disponível ({', '.join(self._modelos)}). "
            "Defina GEMINI_MODEL no .env com um ID válido para a sua chave.",
            response=httpx.Response(404, request=httpx.Request("POST", BASE_URL)),
            body=None,
        )

    # -- uma chamada HTTP (com retry de 429/5xx) -----------------------------
    def _chamar(self, modelo, corpo):
        import anthropic

        url = f"{BASE_URL}/models/{modelo}:generateContent"
        requisicao = httpx.Request("POST", url)
        for tentativa in range(1, 4):
            try:
                with self._sem:
                    # read=150s: ler um PDF escaneado leva ~30s; passar disso e
                    # modelo travado -- melhor cair pro proximo (ver _gerar) que
                    # esperar 5 min numa tentativa so.
                    with httpx.Client(timeout=httpx.Timeout(connect=20.0, read=150.0, write=300.0, pool=60.0)) as http:
                        resp = http.post(url, headers={"x-goog-api-key": _chave()}, json=corpo)
            except httpx.TimeoutException as e:
                raise anthropic.APITimeoutError(request=requisicao) from e
            except httpx.TransportError as e:
                raise anthropic.APIConnectionError(request=requisicao) from e

            if resp.status_code == 200:
                return self._resposta(resp, requisicao)

            msg = _mensagem_erro(resp)
            if resp.status_code == 404:
                raise _ModeloIndisponivel("modelo não encontrado")
            if resp.status_code == 429:
                if re.search(r"limit:\s*0\b", msg):
                    raise _ModeloIndisponivel("sem cota neste modelo")
                espera = _retry_delay(resp) or 10.0 * tentativa
                if tentativa < 3 and espera <= 75:
                    time.sleep(espera + 1)
                    continue
                raise anthropic.RateLimitError(f"Gemini 429: {msg}", response=resp, body=None)
            if resp.status_code >= 500:
                # 2 tentativas por modelo: se ele continua sobrecarregado, e
                # mais rapido cair pro proximo modelo (ver _gerar) do que ficar
                # esperando este.
                if tentativa < 2:
                    time.sleep(5 * tentativa)
                    continue
                raise anthropic.InternalServerError(f"Gemini {resp.status_code}: {msg}", response=resp, body=None)
            if resp.status_code in (401, 403) or (resp.status_code == 400 and "API key" in msg):
                raise anthropic.AuthenticationError(
                    f"Gemini recusou a chave (HTTP {resp.status_code}): {msg} — confira GEMINI_API_KEY.",
                    response=resp,
                    body=None,
                )
            # 400 e demais: se for o schema, o texto precisa conter "json_schema"
            # pra o call_batch cair no modo "JSON pedido no prompt".
            if re.search(r"schema", msg, re.I):
                msg = "json_schema rejeitado pelo Gemini: " + msg
            raise anthropic.BadRequestError(f"Gemini {resp.status_code}: {msg}", response=resp, body=None)
        raise anthropic.InternalServerError("Gemini: tentativas esgotadas", response=resp, body=None)

    # -- resposta do Gemini -> objeto com .content[i].type/.text -------------
    def _resposta(self, resp, requisicao):
        import anthropic

        dados = resp.json()
        candidatos = dados.get("candidates") or []
        if not candidatos:
            motivo = (dados.get("promptFeedback") or {}).get("blockReason") or "sem candidatos"
            raise anthropic.BadRequestError(f"Gemini não devolveu resposta ({motivo})", response=resp, body=None)
        partes = (candidatos[0].get("content") or {}).get("parts") or []
        texto = "".join(p.get("text", "") for p in partes if not p.get("thought"))
        if candidatos[0].get("finishReason") == "MAX_TOKENS":
            print("     [aviso] resposta do Gemini cortada por limite de tokens (GEMINI_MAX_OUTPUT_TOKENS).")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=texto)])
