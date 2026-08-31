#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Leitura de arquivos de uma pasta do SharePoint via Microsoft Graph API,
usando acesso de aplicativo (client credentials -- sem login por usuario).

Espera uma pasta raiz no SharePoint com uma subpasta por cliente (mesmo
formato que o upload manual do sistema ja usa). So faz LEITURA -- nada aqui
grava ou apaga nada no SharePoint.

Configuracao via variaveis de ambiente (mesmo .env que o preencher_peticao.py
ja carrega):
    SHAREPOINT_TENANT_ID     Id do tenant do Azure AD
    SHAREPOINT_CLIENT_ID     Id do aplicativo registrado
    SHAREPOINT_CLIENT_SECRET Segredo do cliente do aplicativo
    SHAREPOINT_HOSTNAME      Ex: contoso.sharepoint.com
    SHAREPOINT_SITE_PATH     Ex: /sites/Juridico
    SHAREPOINT_ROOT_FOLDER   Caminho da pasta raiz dos clientes dentro da
                              biblioteca de documentos, ex:
                              "Documentos Partilhados/Clientes"

Veja COMO_USAR.txt para o passo a passo de como registrar o aplicativo no
Azure e conceder acesso ao site especifico (permissao Sites.Selected).
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

REQUIRED_VARS = [
    "SHAREPOINT_TENANT_ID",
    "SHAREPOINT_CLIENT_ID",
    "SHAREPOINT_CLIENT_SECRET",
    "SHAREPOINT_HOSTNAME",
    "SHAREPOINT_SITE_PATH",
    "SHAREPOINT_ROOT_FOLDER",
]

_token_cache = {"value": None, "expires_at": 0.0}
_site_id_cache = {"value": None}


class SharePointError(RuntimeError):
    """Erro de configuracao ou de comunicacao com o SharePoint/Graph, com
    mensagem ja pronta para mostrar ao usuario."""


def config_error():
    """Retorna uma mensagem em portugues se faltar configuracao, ou None se
    estiver tudo certo -- mesmo padrao de app.api_key_error()."""
    faltando = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if faltando:
        return (
            "A conexão com o SharePoint não está configurada. Faltam as variáveis "
            "no .env: " + ", ".join(faltando) + ". Veja o arquivo COMO_USAR.txt."
        )
    return None


def _require_config(*names):
    valores, faltando = [], []
    for n in names:
        v = os.environ.get(n)
        if not v:
            faltando.append(n)
        valores.append(v)
    if faltando:
        raise SharePointError(
            "Faltam variáveis de ambiente do SharePoint: " + ", ".join(faltando)
            + ". Veja o arquivo COMO_USAR.txt."
        )
    return tuple(valores)


def _request_json(url, method="GET", data=None, headers=None, timeout=30):
    req_headers = dict(headers or {})
    body = None
    if data is not None:
        body = urlencode(data).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            err_json = json.loads(e.read().decode("utf-8"))
            err = err_json.get("error")
            detail = err.get("message", "") if isinstance(err, dict) else (
                err_json.get("error_description") or err or ""
            )
        except Exception:
            pass
        raise SharePointError(
            f"Erro ao falar com o SharePoint/Microsoft Graph (HTTP {e.code}): "
            f"{detail or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise SharePointError(f"Não foi possível conectar ao Microsoft Graph: {e.reason}") from e


def _get_token():
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["value"]
    tenant, client_id, client_secret = _require_config(
        "SHAREPOINT_TENANT_ID", "SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET"
    )
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
    }
    result = _request_json(url, method="POST", data=data)
    _token_cache["value"] = result["access_token"]
    _token_cache["expires_at"] = now + int(result.get("expires_in", 3600))
    return _token_cache["value"]


def _graph_get(url):
    token = _get_token()
    return _request_json(url, headers={"Authorization": f"Bearer {token}"})


def _site_id():
    if _site_id_cache["value"]:
        return _site_id_cache["value"]
    hostname, site_path = _require_config("SHAREPOINT_HOSTNAME", "SHAREPOINT_SITE_PATH")
    site_path = "/" + site_path.strip("/")
    data = _graph_get(f"{GRAPH_BASE}/sites/{hostname}:{site_path}")
    _site_id_cache["value"] = data["id"]
    return data["id"]


def _iter_all(url):
    while url:
        data = _graph_get(url)
        for item in data.get("value", []):
            yield item
        url = data.get("@odata.nextLink")


def list_client_folders():
    """Lista o nome das subpastas (uma por cliente) da pasta raiz configurada."""
    site_id = _site_id()
    (root,) = _require_config("SHAREPOINT_ROOT_FOLDER")
    root = root.strip("/")
    url = f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{quote(root)}:/children?$top=999&$select=name,folder"
    return sorted(item["name"] for item in _iter_all(url) if "folder" in item)


def download_client_folder(nome: str, dest_dir: Path):
    """Baixa todos os arquivos (recursivamente) da subpasta `nome` para
    `dest_dir`, preservando a estrutura de subpastas -- mesmo formato que o
    upload manual do sistema ja produz, para o resto do pipeline
    (preencher_peticao.find_documents em diante) funcionar sem mudancas."""
    site_id = _site_id()
    (root,) = _require_config("SHAREPOINT_ROOT_FOLDER")
    root = root.strip("/")
    path = f"{root}/{nome}" if root else nome
    try:
        folder_item = _graph_get(f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{quote(path)}")
    except SharePointError as e:
        if "HTTP 404" in str(e):
            raise SharePointError(f"Pasta do cliente '{nome}' não encontrada no SharePoint.") from e
        raise
    if "folder" not in folder_item:
        raise SharePointError(f"'{nome}' não é uma pasta no SharePoint.")
    _baixar_conteudo(site_id, folder_item["id"], dest_dir)


def _baixar_conteudo(site_id, folder_id, dest_dir: Path):
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = f"{GRAPH_BASE}/sites/{site_id}/drive/items/{folder_id}/children?$top=999"
    for item in _iter_all(url):
        nome_item = item["name"]
        if "folder" in item:
            _baixar_conteudo(site_id, item["id"], dest_dir / nome_item)
        elif "file" in item:
            download_url = item.get("@microsoft.graph.downloadUrl")
            if download_url:
                _baixar_arquivo(download_url, dest_dir / nome_item)


def _baixar_arquivo(url, destino: Path):
    with urllib.request.urlopen(url, timeout=120) as resp:
        destino.write_bytes(resp.read())
