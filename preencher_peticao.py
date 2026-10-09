#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Preenche o Modelo de Peticao Inicial (Reparadoras) a partir dos documentos
de um cliente, usando a API da Anthropic (Claude).

Uso:
    python preencher_peticao.py "C:\\caminho\\para\\pasta do cliente"

Opcoes:
    --output CAMINHO.html   onde salvar o HTML preenchido (padrao: dentro da
                             propria pasta do cliente)
    --template CAMINHO.html modelo base (padrao: o modelo desta pasta)
    --model MODEL_ID         modelo da API a usar (padrao: claude-haiku-4-5)
    --dry-run                lista os arquivos e os lotes que seriam enviados,
                              sem chamar a API (para testar antes de gastar)

Requer a variavel de ambiente ANTHROPIC_API_KEY configurada com uma chave
valida, obtida em https://console.anthropic.com/settings/keys

No Windows (PowerShell), para configurar permanentemente:
    setx ANTHROPIC_API_KEY "sua-chave-aqui"
(feche e abra o terminal de novo depois de rodar o setx)
"""
import argparse
import base64
import csv
import datetime
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

# O console do Windows (cp1252) nao tem varios caracteres usados nas mensagens
# (ex: ⚠) e derruba o script com UnicodeEncodeError bem no final, depois de já
# ter gerado o HTML -- forcar UTF-8 evita esse crash e exibe os acentos certo.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    print("Falta instalar dependencias. Rode:")
    print('  .venv\\Scripts\\python.exe -m pip install anthropic beautifulsoup4')
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = SCRIPT_DIR / "Modelo petição inicial reparadoras (2).html"


def _load_dotenv():
    """Le SCRIPT_DIR/.env (formato CHAVE=valor) e injeta em os.environ.

    Evita depender de `setx`/variavel de ambiente do Windows, que so tem
    efeito em janelas/processos abertos DEPOIS do comando -- o Explorer nao
    recarrega sozinho, entao um .bat clicado direto no Explorer continua
    sem enxergar a variavel ate reiniciar o computador. Um arquivo local
    lido a cada execucao nao tem esse problema.
    """
    env_path = SCRIPT_DIR / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()
# DEFAULT_MODEL pode vir do .env (ex: OMNIROUTE_MODEL=adapta-web/auto para
# rotear pelo OmniRoute em vez de chamar a Anthropic direto). Se
# ANTHROPIC_BASE_URL estiver setado no .env, o SDK da anthropic ja usa esse
# endereco automaticamente -- nao precisa de nenhum outro ajuste aqui.
DEFAULT_MODEL = os.environ.get("OMNIROUTE_MODEL") or "claude-haiku-4-5"

MAX_BATCH_BYTES_B64 = 12 * 1024 * 1024  # ~12MB base64 por lote (margem sob o limite de 32MB da API)
MAX_FILES_PER_BATCH = 8

SUPPORTED_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
# Extensoes aceitas so no UPLOAD (nunca mandadas direto pra IA) -- .xlsx e
# convertido pra .png por _converter_planilhas_para_imagem() antes de
# find_documents() rodar; sem essa lista, app.py descartaria o arquivo no
# upload antes mesmo dessa conversao acontecer.
UPLOAD_EXTRA_EXT = {".xlsx"}

MUNICIPIOS_SP_PATH = SCRIPT_DIR / "municipios_sp_tjsp.csv"


# ---------------------------------------------------------------------------
# Comarca dinamica (TJSP) -- descobre automaticamente qual comarca atende o
# municipio do cliente, em vez de depender de alguem preencher isso a mao.
# So cobre SP (onde o escritorio atua) e falha em silencio (retorna None) se
# qualquer coisa der errado -- o campo so fica em branco pra preencher manual,
# nunca trava o resto da extracao.
# ---------------------------------------------------------------------------

def _normalize_text(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


def _load_municipios_sp():
    if not MUNICIPIOS_SP_PATH.is_file():
        return {}
    result = {}
    with open(MUNICIPIOS_SP_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            nome = row.get("municipio_tjsp_corrigido") or row.get("municipio_tjsp")
            codigo = row.get("id_municipio_tjsp")
            if nome and codigo:
                result[_normalize_text(nome)] = codigo
    return result


# Em hospedagem serverless (Vercel seta VERCEL=1) o disco do deploy e
# somente leitura -- so /tmp aceita escrita. Sem isso o log so falharia
# silenciosamente (_log_tjsp engole a excecao), o que ja seria inofensivo,
# mas em /tmp ele de fato funciona pra ajudar a diagnosticar algo no ar.
_TJSP_DEBUG_LOG = (Path("/tmp") if os.environ.get("VERCEL") else SCRIPT_DIR) / "_tjsp_debug.log"


def _log_tjsp(msg):
    """Log temporario de diagnostico das consultas ao TJSP (comarca/foro) --
    nunca lanca excecao nem afeta o resultado, so ajuda a investigar falhas
    intermitentes de rede sem precisar pedir print pro usuario."""
    try:
        with open(_TJSP_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def lookup_comarca_sp(municipio, uf):
    """Consulta a busca oficial do TJSP para achar a comarca responsavel por
    um municipio de SP. Retorna None (nunca lanca excecao) se a UF nao for
    SP, o municipio nao for reconhecido, a rede falhar, ou a pagina do TJSP
    mudar de formato -- nesses casos o campo fica em branco para
    preenchimento manual, como antes."""
    if not uf or _normalize_text(uf) != "sp":
        _log_tjsp(f"comarca_sp: uf={uf!r} nao e SP, abortando")
        return None
    if not municipio:
        _log_tjsp("comarca_sp: municipio vazio, abortando")
        return None
    codigo = _load_municipios_sp().get(_normalize_text(municipio))
    if not codigo:
        _log_tjsp(f"comarca_sp: municipio={municipio!r} nao encontrado no CSV")
        return None
    try:
        req = urllib.request.Request(
            "https://www.tjsp.jus.br/ListaTelefonica/RetornarResultadoBusca",
            data=f"parmsEntrada={codigo}&codigoTipoBusca=1".encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = html.unescape(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        _log_tjsp(f"comarca_sp: EXCECAO na requisicao (municipio={municipio!r}, codigo={codigo}): {e!r}")
        return None

    m = re.search(r"Comarca\s+([^-<]+?)\s*-\s*\d", body)
    if not m:
        _log_tjsp(f"comarca_sp: resposta sem 'Comarca ...' reconhecivel (municipio={municipio!r}); corpo[:200]={body[:200]!r}")
        return None
    resultado = m.group(1).strip()
    _log_tjsp(f"comarca_sp: OK municipio={municipio!r} -> {resultado!r}")
    return resultado


def lookup_foro_capital_sp(cep):
    """Consulta a ferramenta oficial 'Competencia Territorial - Capital' do
    TJSP (https://www.tjsp.jus.br/app/CompetenciaTerritorial) para achar o
    Foro Regional responsavel por um CEP da cidade de Sao Paulo (que tem
    dezenas de foros regionais por bairro, sem uma regra fixa por
    municipio). Retorna None (nunca lanca excecao) se o CEP nao for
    reconhecido, a rede falhar, ou a pagina mudar de formato -- nesses casos
    o campo fica em branco para preenchimento manual, como antes."""
    if not cep:
        _log_tjsp("foro_capital: cep vazio, abortando")
        return None
    cep_digitos = re.sub(r"\D", "", cep)
    if len(cep_digitos) != 8:
        _log_tjsp(f"foro_capital: cep={cep!r} nao tem 8 digitos ({cep_digitos!r}), abortando")
        return None
    try:
        req = urllib.request.Request(
            "https://www.tjsp.jus.br/APP/CompetenciaTerritorial/Home/ListarCompetencias",
            data=f"busca={cep_digitos}&tipoBusca=1".encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        _log_tjsp(f"foro_capital: EXCECAO na requisicao (cep={cep_digitos}): {e!r}")
        return None

    foros = set(re.findall(r'"Foro"\s*:\s*"([^"]+)"', body))
    if len(foros) != 1:
        # Sem resultado, ou resultado ambiguo (mais de um foro pro mesmo CEP
        # -- nao deveria acontecer, mas por seguranca nao arriscamos escolher).
        _log_tjsp(f"foro_capital: cep={cep_digitos} -> {len(foros)} foro(s) encontrado(s) {foros!r}; corpo[:200]={body[:200]!r}")
        return None
    resultado = next(iter(foros)).strip()
    _log_tjsp(f"foro_capital: OK cep={cep_digitos} -> {resultado!r}")
    return resultado


def resolve_comarca_foro(data):
    """Texto pronto pra o campo comarca_foro, ou None se precisar de
    preenchimento manual (fora de SP, ou busca sem sucesso)."""
    comarca = lookup_comarca_sp(data.get("cliente_cidade"), data.get("cliente_uf"))
    if not comarca:
        return None
    if _normalize_text(comarca) == "sao paulo":
        foro = lookup_foro_capital_sp(data.get("cliente_cep"))
        if not foro:
            return None
        # O artigo certo (do/da/de + nome) varia por foro e nao da pra
        # deduzir com seguranca so pelo nome -- por isso o campo continua
        # .fill (editavel) e usa "DE" como conector neutro; revisar antes de
        # protocolar, igual aos demais campos preenchidos pela IA.
        return f"REGIONAL DE {foro.upper()} DA COMARCA DE SÃO PAULO"
    return f"DA COMARCA DE {comarca.upper()}/SP"

MESES_PT = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]

# ---------------------------------------------------------------------------
# Schema de extracao — cada chave vira um campo do JSON que a IA devolve.
# ---------------------------------------------------------------------------

def _nullable_string():
    # A API rejeita schemas com muitos campos de tipo uniao (["string","null"]):
    # "Schemas contains too many parameters with union types". Por isso usamos
    # string simples e tratamos "" como "nao encontrado" no restante do codigo.
    return {"type": "string"}


def _nullable_bool():
    return {"type": "boolean"}


SCHEMA_PROPERTIES = {
    "cliente_nome_completo": _nullable_string(),
    "cliente_estado_civil": _nullable_string(),
    "cliente_profissao": _nullable_string(),
    "cliente_rg": _nullable_string(),
    "cliente_cpf": _nullable_string(),
    "cliente_endereco_logradouro": _nullable_string(),
    "cliente_endereco_numero": _nullable_string(),
    "cliente_endereco_complemento": _nullable_string(),
    "cliente_bairro": _nullable_string(),
    "cliente_cidade": _nullable_string(),
    "cliente_uf": _nullable_string(),
    "cliente_cep": _nullable_string(),
    "cliente_email": _nullable_string(),
    "cliente_idade": _nullable_string(),
    "operadora_nome": _nullable_string(),
    "operadora_cnpj": _nullable_string(),
    "operadora_logradouro": _nullable_string(),
    "operadora_numero": _nullable_string(),
    "operadora_bairro": _nullable_string(),
    "operadora_cidade": _nullable_string(),
    "operadora_uf": _nullable_string(),
    "operadora_cep": _nullable_string(),
    "operadora_email": _nullable_string(),
    "carteirinha_numero": _nullable_string(),
    "peso_perdido_kg": _nullable_string(),
    "comorbidades_fisicas_1": _nullable_string(),
    "comorbidades_fisicas_2": _nullable_string(),
    "comorbidades_psicologicas": _nullable_string(),
    "menciona_lipedema": _nullable_bool(),
    "lipedema_paragrafo_1": _nullable_string(),
    "lipedema_paragrafo_2": _nullable_string(),
    "medico_nome_crm": _nullable_string(),
    "procedimentos_total_no_laudo": {"type": "integer"},
    "procedimentos_lista": {"type": "array", "items": {"type": "string"}},
    "psicologa_nome_crp": _nullable_string(),
    "danos_psicologicos_paragrafo": _nullable_string(),
    "danos_morais_laudo_1": _nullable_string(),
    "danos_morais_laudo_2": _nullable_string(),
    "danos_morais_relacao": _nullable_string(),
    "fatos_lista": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "data": {"type": "string"},  # AAAA-MM-DD; "" se o documento nao tiver data
                "texto": {"type": "string"},
            },
            "required": ["data", "texto"],
            "additionalProperties": False,
        },
    },
    "recebeu_negativa_formal": _nullable_bool(),
    "tem_declaracao_gratuidade": _nullable_bool(),
    "gratuidade_documentos_total": {"type": "integer"},
    "gratuidade_documentos_lista": {"type": "array", "items": {"type": "string"}},
    "gratuidade_renda_mensal": _nullable_string(),
    "gratuidade_percentual_comprometido": _nullable_string(),
    "gratuidade_saldo_residual": _nullable_string(),
    "observacoes_divergencias": _nullable_string(),
    "carteirinha_frente_arquivo": _nullable_string(),
    "carteirinha_frente_pagina": _nullable_string(),
    "carteirinha_verso_arquivo": _nullable_string(),
    "carteirinha_verso_pagina": _nullable_string(),
    "laudo_cirurgiao_arquivo": _nullable_string(),
    "laudo_comorbidades_pagina": _nullable_string(),
    "laudo_comorbidades_trecho": _nullable_string(),
    "laudo_procedimentos_pagina": _nullable_string(),
    "laudo_urgencia_pagina": _nullable_string(),
    "laudo_urgencia_trecho": _nullable_string(),
    "laudo_psicologico_arquivo": _nullable_string(),
    "laudo_psicologico_conclusao_pagina": _nullable_string(),
    "laudo_psicologico_conclusao_trecho": _nullable_string(),
    "gratuidade_planilha_arquivo": _nullable_string(),
}

SCHEMA = {
    "type": "object",
    "properties": SCHEMA_PROPERTIES,
    "required": list(SCHEMA_PROPERTIES.keys()),
    "additionalProperties": False,
}

# Campos de texto "de historia" onde o estilo formal da peticao importa.
SYSTEM_PROMPT = """Você é um assistente jurídico que extrai dados de documentos de um caso \
real para preencher uma petição inicial contra uma operadora de plano de saúde, pedindo a \
cobertura de cirurgias plásticas reparadoras pós-bariátricas (a cliente já fez cirurgia \
bariátrica antes e perdeu peso; agora precisa de cirurgias reparadoras — dermolipectomia, \
mastopexia, etc. — e o plano negou ou não respondeu).

Você recebe vários documentos (PDFs e imagens): documentos pessoais da cliente (RG, CNH, \
comprovante de residência, procuração), a carteirinha do plano de saúde, laudos médicos \
(cirurgião plástico e psicólogo/psiquiatra), comprovantes de contato com a operadora \
(e-mails, WhatsApp, ligações) e documentos administrativos do escritório (declaração de \
pobreza/hipossuficiência, contrato de honorários, termos de ciência).

Se, entre os arquivos recebidos, houver algo que já parece uma petição inicial pronta/protocolada \
(com cabeçalho de "EXCELENTÍSSIMO SENHOR..." ou trechos redigidos em estilo jurídico formal em \
terceira pessoa, "a Requerente...") — IGNORE esse arquivo por completo para todos os campos. \
Ele não é uma fonte válida: extraia sempre dos documentos originais (laudos, RG/CNH, \
comprovantes, mensagens), nunca copiando ou parafraseando de uma petição já redigida, mesmo que \
pareça mais completa ou mais bem escrita — o objetivo é uma extração independente dos \
documentos primários, não uma cópia de um texto já pronto.

Extraia SOMENTE o que está literalmente escrito nos documentos. Nunca invente números de \
RG, CPF, CNPJ, CEP ou carteirinha. Se um dado não aparecer em nenhum documento, deixe o \
campo como uma string vazia "" (nunca use null, nunca escreva "não encontrado" dentro do \
campo — apenas ""). NUNCA escreva um número (RG, CPF, CNPJ, CEP, carteirinha etc.) com \
asterisco ou "***" no lugar de dígitos que você não conseguiu ler — isso é pior do que \
inventar, porque vira texto literal errado dentro da petição (ex: "60.***.***/0001-0*" NÃO é \
um CNPJ válido para colocar no documento). Se o número estiver parcialmente ilegível \
(borrado, cortado, com reflexo), duas opções, nunca uma mistura das duas: (a) se você \
conseguir ler TODOS os dígitos com confiança, mesmo que a imagem não esteja perfeita, escreva \
o número completo; (b) se não conseguir ler algum dígito com confiança, deixe o campo \
inteiro como "" e explique em observacoes_divergencias qual documento tem esse dado parcialmente \
legível, para conferência manual.

ATENÇÃO — documentos de procuração/formulários preenchidos por assinatura eletrônica (ex: \
ZapSign) têm um histórico de erro de digitação no RG e no CEP (um dígito a mais ou trocado). \
Se o RG ou o CEP aparecer tanto na procuração quanto em outro documento (CNH para RG; \
comprovante de residência, cartão do plano ou declaração para CEP), PREFIRA o valor do outro \
documento, não o da procuração. Se só a procuração tiver o dado, use-o mas descreva em \
observacoes_divergencias que a fonte foi só a procuração e por isso merece conferência manual. \
Se dois documentos (nenhum deles a procuração) divergirem entre si, reporte ambos os valores \
nos textos e explique em observacoes_divergencias — não escolha sozinho.

RG (cliente_rg) — este campo costuma ter documentos de identidade agrupados no mesmo lote \
(CNH, Procuração, e às vezes um RG digitalizado à parte) exatamente para você poder CRUZAR o \
número entre eles antes de responder. Leia o RG em cada documento de identidade disponível \
neste lote, compare os números dígito a dígito e: se dois ou mais concordarem, use esse valor \
com confiança; se só um documento tiver o RG legível, use-o mas registre em \
observacoes_divergencias que não houve confirmação cruzada; se a imagem estiver borrada, com \
reflexo ou pouco legível a ponto de você não ter certeza de cada dígito, NÃO invente um número \
plausível — registre o que conseguir ler com clareza (ex: os dígitos legíveis) e sinalize \
claramente em observacoes_divergencias que o documento precisa ser conferido manualmente por \
um humano, em vez de arriscar um número errado.

ATENÇÃO ESPECIAL — CNH (Carteira Nacional de Habilitação): a CNH brasileira tem VÁRIOS números \
diferentes impressos, e é fácil confundir um pelo outro. O RG está especificamente no campo \
rotulado "4c DOC IDENTIDADE / ÓRGÃO EMISSOR/UF" (contém somente os dígitos do RG, seguidos da \
sigla do órgão emissor e da UF — copie somente os dígitos que aparecem nesse campo específico, \
sem hífen nem dígito adicional que não esteja impresso ali). NÃO use para o RG: (a) o número \
grande impresso na vertical na lateral esquerda do cartão (esse é um número de \
documento/segurança da CNH, não o RG); (b) o campo "5 Nº REGISTRO" (esse é o número de registro \
da própria habilitação, não o RG); (c) nenhum número da zona de leitura mecânica (MRZ, as linhas \
de texto tipo "I<BRA..." no rodapé). Copie o RG exatamente como aparece no campo 4c, sem \
adicionar dígitos de outros campos próximos.

NOME COMPLETO DA CLIENTE (cliente_nome_completo) — aplique a MESMA lógica de cruzamento do \
RG: leia o nome em CADA documento de identidade disponível no lote (RG, CNH, Procuração) letra \
por letra, prestando atenção especial ao INÍCIO do nome (é comum perder a primeira sílaba ao \
ler rápido, ex: ler "Tefania" onde está escrito "Estefania" ou "Stefanie"). Se dois ou mais \
documentos concordarem, use esse valor. Se houver divergência de grafia entre documentos (ex: \
com/sem acento, "Bruna" vs "Brunna"), prefira a grafia do RG; se o RG não estiver disponível ou \
legível, use a CNH. Nunca abrevie nem corte o nome — copie o nome COMPLETO, com todos os \
sobrenomes, exatamente como impresso no documento escolhido.

CARTEIRINHA DO PLANO (carteirinha_numero) — copie o número EXATAMENTE como aparece na imagem \
do cartão/carteirinha, dígito por dígito E com os MESMOS espaços/agrupamentos visuais entre os \
blocos de dígitos que aparecem impressos no cartão (se o cartão separa os dígitos em blocos com \
espaços entre eles, incluindo um possível dígito isolado no final, reproduza essa mesma \
separação — não junte tudo numa sequência só nem corte o último dígito). Se houver mais \
de um documento com esse número no mesmo lote (ex: cartão do plano e verso da carteirinha), \
confira se todos concordam antes de responder; se não concordarem, reporte o mais completo e \
explique em observacoes_divergencias. Não confunda com outros números que também aparecem perto \
da carteirinha (código ANS da operadora, CNS, datas de vigência/nascimento).

ENDEREÇO DA CLIENTE (cliente_endereco_logradouro, cliente_endereco_numero, \
cliente_endereco_complemento, cliente_bairro, cliente_cidade, cliente_uf, cliente_cep) — a \
cidade e a UF daqui são usadas pelo sistema para descobrir automaticamente a comarca \
responsável pelo processo, então a fonte certa importa: 1) Priorize sempre o Comprovante de \
Residência quando ele existir entre os documentos — é a fonte mais confiável e atual do \
endereço. 2) Se não houver Comprovante de Residência na pasta, use o endereço que consta na \
Procuração (mas lembre-se do aviso acima sobre erros de digitação nesse documento — se outro \
documento qualquer, como a CNH, também tiver o endereço ou parte dele, prefira-o). 3) Se nem \
Comprovante de Residência nem Procuração tiverem o endereço, procure em qualquer outro \
documento disponível (declaração de pobreza, contrato, etc.) antes de deixar o campo vazio. \
Registre em observacoes_divergencias de qual documento o endereço final veio, especialmente \
quando não veio do Comprovante de Residência.

IDADE DA CLIENTE (cliente_idade) — SOMENTE preencha se houver, em algum documento do lote (RG, \
CNH, certidão de nascimento, procuração, laudo), a data de nascimento OU a idade já indicada por \
extenso. Se só houver a data de nascimento, calcule a idade em anos completos tomando como \
referência a data de hoje. Formato: só o número seguido de "anos" (ex: "16 anos"). Se não houver \
nenhuma fonte para calcular, deixe "".

OPERADORA/REQUERIDA (operadora_nome, operadora_cnpj, operadora_logradouro, operadora_numero, \
operadora_bairro, operadora_cidade, operadora_uf, operadora_cep, operadora_email) — são os \
dados cadastrais da PRÓPRIA OPERADORA DE PLANO DE SAÚDE (a empresa que vai ser processada, não \
a cliente). A fonte mais confiável costuma ser um print/captura de tela de consulta pública, que \
pode aparecer solto na pasta com nome genérico tipo "imagem.png" — reconheça pelo CONTEÚDO, não \
pelo nome do arquivo: (1) um print do "CADASTRO NACIONAL DA PESSOA JURÍDICA" / "COMPROVANTE DE \
INSCRIÇÃO E DE SITUAÇÃO CADASTRAL" da Receita Federal, com campos como "NÚMERO DE INSCRIÇÃO" \
(= CNPJ) e "NOME EMPRESARIAL" (= razão social, use esse para operadora_nome — não o "TÍTULO DO \
ESTABELECIMENTO/NOME DE FANTASIA"). O endereço nesse card vem em DUAS linhas separadas, as duas \
IMPORTANTES — não pare na primeira: uma linha de cima com "LOGRADOURO", "NÚMERO" e \
"COMPLEMENTO" (ex: "R SANTOS DUMONT" / "2705"), e uma linha de baixo com "CEP", \
"BAIRRO/DISTRITO", "MUNICÍPIO" e "UF". Preencha operadora_logradouro e operadora_numero com os \
valores da linha de CIMA, e operadora_cep/operadora_bairro/operadora_cidade/operadora_uf com os \
da linha de BAIXO — é um erro comum ler só a linha de baixo e deixar logradouro/número vazios \
mesmo eles estando visíveis logo acima; (2) um \
print de consulta de operadora na ANS, com campos como "Razão Social", "Registro ANS" e "CNPJ" \
— serve para confirmar/cruzar operadora_nome e operadora_cnpj, mas normalmente não traz \
endereço completo (nesse caso, o endereço continua vindo do print da Receita Federal, se \
houver). Se esses prints não existirem na pasta, procure o CNPJ/endereço da operadora em \
qualquer outro documento disponível (carteirinha, papel timbrado de carta de negativa, rodapé \
de e-mail da operadora etc.) antes de deixar os campos vazios — mas prefira sempre o print de \
cadastro oficial quando ele existir, por ser a fonte mais completa e atualizada. \
operadora_email é o e-mail de contato da operadora (costuma vir de e-mails trocados com ela ou \
da carteirinha), não o e-mail do print de CNPJ. CONFIRME SEMPRE antes de preencher: em uma troca \
de e-mail, o endereço da operadora normalmente aparece no campo "Para:"/"To:" quando é a cliente \
(ou o escritório) que está enviando, ou no campo "De:"/"From:" quando é a operadora que está \
respondendo — nunca use o e-mail do escritório de advocacia (ex: domínio do próprio escritório) \
nem o e-mail pessoal da cliente para este campo, mesmo que apareçam na mesma mensagem. Se houver \
mais de um e-mail plausível da operadora em documentos diferentes e eles não baterem, registre a \
divergência em observacoes_divergencias em vez de escolher um sem certeza.

Para os campos booleanos (menciona_lipedema, recebeu_negativa_formal, tem_declaracao_gratuidade), \
responda sempre true ou false (nunca deixe vazio) — use false quando não houver evidência clara \
nos documentos.

Para os campos de texto narrativo, escreva em português formal, no estilo de uma petição \
jurídica brasileira (terceira pessoa, "a Requerente"), seguindo estes exemplos de estilo \
(NÃO copie o conteúdo do exemplo — é só para mostrar o registro/tom esperado). IMPORTANTE: o \
exemplo mostra apenas o TOM formal — o CONTEÚDO clínico (partes do corpo citadas, achados, \
consequências) tem que vir EXCLUSIVAMENTE do laudo real desta cliente. Se o laudo cita mamas, \
abdome, dorso, glúteos, braços, coxas e região íntima, a resposta tem que citar exatamente \
essas partes — não generalize para outra lista de regiões nem troque por partes do corpo de \
outro caso. O mesmo vale para consequências/sintomas (ex: intertrigo, dor cervical, baixa \
autoestima): só inclua o que o laudo desta cliente realmente descreve.

- comorbidades_fisicas_1 (deformidades físicas decorrentes da perda de peso, com base no \
laudo do cirurgião): "abdome em avental, ptoses mamárias deformantes e assimétricas, \
associadas à importante flacidez cutânea [...], bem como distrofias cutâneas e subcutâneas \
em região cervical, torácica, lombar, sacral, glútea, braquial, crural e coxas \
bilateralmente."

- comorbidades_fisicas_2 (impacto funcional/higiênico da flacidez, com base no laudo do \
cirurgião): "apresenta flacidez cutânea excessiva, que evolui constantemente com quadros de \
intertrigo e celulites bacterianas nas dobras cutâneas [...] necessitando do uso contínuo de \
cremes, pomadas, antissépticos e desodorantes corporais."

- comorbidades_psicologicas (impacto emocional, com base no laudo psicológico): "a \
Requerente encontra-se em acompanhamento psicológico especializado, diante dos relevantes \
prejuízos emocionais decorrentes das sequelas pós-bariátricas, apresentando baixa \
autoestima, insatisfação com a própria imagem corporal [...]"

REGRA DE NÃO REPETIÇÃO DAS COMORBIDADES (vale para os três campos acima): cada achado aparece em \
UM SÓ campo. comorbidades_fisicas_1 fica restrito às DEFORMIDADES e às regiões do corpo afetadas \
(abdome, mamas, dorso, glúteos, braços, coxas, região íntima etc.) — NÃO inclua aqui feridas, \
intertrigo, mau odor, dificuldade de higiene nem sofrimento emocional. comorbidades_fisicas_2 \
fica restrito às CONSEQUÊNCIAS FUNCIONAIS/HIGIÊNICAS da flacidez (intertrigo, feridas nas dobras, \
celulites, higiene, sudorese, atrito, uso de cremes) e NÃO repete as regiões nem as deformidades \
já citadas em comorbidades_fisicas_1. comorbidades_psicologicas fica restrito ao IMPACTO \
EMOCIONAL (autoestima, humor, imagem corporal, convívio social) e NÃO repete achados físicos. \
Antes de responder, releia os três textos: se uma mesma região ou sintoma aparecer em mais de um \
deles, mantenha somente no campo mais adequado e remova dos outros. Os três campos são \
parágrafos separados na petição, por isso cada um deve acrescentar informação nova.

- danos_psicologicos_paragrafo (diagnóstico psicológico formal com CID, se houver, do laudo \
psicológico): "a Requerente [...] apresenta problemas psicológicos, como isolamento social, \
vergonha em demasia, insegurança, baixa autoestima [...], podendo ser classificado como \
Transtorno Depressivo (CID-10 F32) e Transtorno de Ansiedade (CID-10 F41.1)."

- danos_morais_laudo_1 (o que o laudo psicológico documenta sobre o impacto emocional da negativa/interrupção do tratamento, para a seção de danos morais): 1-2 frases citando achados concretos do laudo (ex: quadro depressivo reativo, fobia social, ansiedade).

- danos_morais_laudo_2 (detalhes adicionais do laudo psicológico: autoestima, constrangimento, ansiedade, angústia, isolamento social, imagem corporal, uso de medicação, etc.): 1-2 frases complementares às de danos_morais_laudo_1, sem repetir o mesmo conteúdo.

- danos_morais_relacao (frase curta ligando o que o laudo diz ao caso concreto da Requerente, explicando por que ultrapassa o mero aborrecimento): 1 frase, ex: "compromete diretamente sua saúde psicológica e seu convívio social, revelando alteração anímica concreta".

- lipedema_paragrafo_1 / lipedema_paragrafo_2: só preencha se os documentos mencionarem \
lipedema ou blefarocalazo com comprometimento de campo visual; caso contrário deixe null e \
marque menciona_lipedema como false. ATENÇÃO — não marque true só porque a palavra \
"blefaroplastia" aparece na lista de procedimentos solicitados (várias pacientes pedem \
blefaroplastia estética sem ter blefarocalazo com comprometimento visual). Só marque true se o \
laudo, especificamente, DIAGNOSTICAR blefarocalazo (ou lipedema) NA PACIENTE e descrever \
prejuízo funcional/comprometimento do campo visual decorrente disso, como uma condição de saúde \
à parte — não apenas mais um procedimento estético da lista.

- fatos_lista: um ARRAY de objetos {"data", "texto"}, UM OBJETO PARA CADA parágrafo da narrativa \
cronológica dos fatos. "data" é a data do fato no formato AAAA-MM-DD (ex: "2026-06-24"), copiada \
do documento de origem — use "" somente se o documento realmente não trouxer data nenhuma. "texto" \
é o parágrafo da narrativa. O sistema reordena os itens pela "data", mas você também deve entregá-los \
já em ordem cronológica. NÃO existe limite de quantidade, use quantos parágrafos forem necessários para cobrir \
TODOS os fatos/contatos relevantes encontrados nos documentos, mesmo que sejam 5, 7 ou mais. É \
um erro grave resumir/comprimir vários fatos distintos num parágrafo só só para reduzir a \
quantidade — cada fato ou contato distinto (solicitação inicial, cada nova tentativa/reenvio, \
cada ligação, cada resposta ou ausência de resposta da operadora, qualquer outro evento \
relevante contado nos documentos) vira o SEU PRÓPRIO parágrafo/item do array, na ORDEM \
CRONOLÓGICA em que aconteceu. Antes de responder, LEVANTE mentalmente todos os documentos deste \
lote que contam a história (e-mails, prints de WhatsApp, ligações registradas, protocolos, \
cartas de negativa, qualquer comprovante de contato ou andamento) e confirme que cada um deles \
está refletido em pelo menos um item do array — não deixe nenhum documento relevante de fora só \
porque já "parece" que a história está completa com menos itens, e não pare no meio: continue \
até o ÚLTIMO fato/contato encontrado nos documentos, não só os primeiros dois ou três. Cada \
item deve citar o CANAL (e-mail, WhatsApp, telefone, aplicativo) e a DATA daquele fato \
específico, quando disponíveis, com o máximo de detalhe que o documento de origem permitir (não \
generalize "entrou em contato" quando o documento tem detalhes concretos do que foi dito/\
respondido). Se o documento tem uma data exata (ex: um print de WhatsApp datado de 29/07, ou um \
e-mail com data no cabeçalho), USE essa data por extenso — nunca escreva algo vago como "em \
data posterior" ou "dias depois" quando a data exata está disponível no documento. Este campo é \
SOMENTE sobre a cronologia de contatos/solicitações com a operadora — NÃO repita aqui a \
descrição de comorbidades/achados médicos (isso já vai em comorbidades_fisicas_1, \
comorbidades_fisicas_2 e comorbidades_psicologicas, campos separados); cada item de fatos_lista \
fala do que ACONTECEU (quem contatou quem, quando, por qual canal, dizendo o quê), não do \
quadro clínico da cliente. Por padrão, escreva como \
se a própria Requerente tivesse feito o contato diretamente ("a Requerente entrou em \
contato...") — só mencione que foi "por meio de representante legal" ou de outra pessoa se \
algum documento mostrar EXPLICITAMENTE que não foi a cliente quem contatou a operadora. Estilo \
de cada item: "Com a referida prescrição médica, a Requerente entrou em contato com a \
Requerida, via e-mail, em [data], solicitando autorização [...]".

- recebeu_negativa_formal: true somente se algum documento mostrar uma negativa POR ESCRITO \
e formal da operadora (carta, e-mail oficial de negativa). Se a cliente só recebeu \
informação verbal/telefônica de que os procedimentos seriam negados, ou nenhuma resposta, \
marque false.

- tem_declaracao_gratuidade: NÃO marque true só porque existe uma declaração de pobreza/\
hipossuficiência assinada nos documentos — esse documento às vezes é assinado por padrão do \
escritório mesmo quando a cliente depois decide, numa conversa (WhatsApp, e-mail, ligação), \
que prefere arcar com as próprias custas. Procure especificamente por essa manifestação da \
cliente sobre QUERER (ou não) pedir a gratuidade/isenção de custas. Se houver uma mensagem \
posterior da cliente dizendo que vai pagar as custas, prevalece essa mensagem e o campo deve \
ser false, mesmo com a declaração assinada. Se não houver nenhuma manifestação clara nem para \
um lado nem para outro, use a existência da declaração assinada como sinal e explique a \
incerteza em observacoes_divergencias.

- gratuidade_documentos_total / gratuidade_documentos_lista: SOMENTE se tem_declaracao_gratuidade \
for true. Erro grave e comum aqui: esquecer um documento que ESTÁ no lote só porque o nome do \
arquivo não bateu na cabeça com o tipo esperado (ex: um arquivo chamado "contracheque.pdf" ou \
"CC.pdf" é o mesmo que holerite; "comprovante de rendimentos" também é holerite ou extrato, \
dependendo do conteúdo). Por isso, faça isto ANTES de contar: LISTE mentalmente (não precisa \
escrever no JSON) o NOME de CADA ARQUIVO deste lote, um por um, sem pular nenhum — só depois de \
ter essa lista completa na cabeça, classifique cada arquivo dela em um dos tipos abaixo (um \
arquivo pode não se encaixar em nenhum, tudo bem, mas não pule nenhum arquivo sem checar). Tipos \
possíveis: (1) declaração de pobreza/hipossuficiência assinada; (2) CTPS (carteira de trabalho, \
inclusive digital); (3) extrato(s) bancário(s) — conta como 1 tipo mesmo se houver vários meses; \
(4) holerite(s)/contracheque(s)/comprovante(s) de pagamento de salário — idem, 1 tipo mesmo com \
vários meses; (5) declaração de imposto de renda/IRPF; (6) planilha demonstrativa de despesas \
mensais (tabela com categorias de gasto e valores, às vezes já convertida de Excel para imagem). \
Primeiro (gratuidade_documentos_total): conte quantos desses 6 tipos você efetivamente encontrou \
depois de classificar TODOS os arquivos do lote, e coloque o número aqui. Depois \
(gratuidade_documentos_lista): um ARRAY com um item de string por tipo contado acima (ex: \
["declaração de pobreza", "CTPS", "extratos bancários", "holerites", "planilha de despesas \
mensais"]) — o array TEM que ter exatamente gratuidade_documentos_total itens, nem a mais nem a \
menos. Antes de responder, confira mais uma vez: para cada tipo que você NÃO incluiu, tem \
certeza de que nenhum arquivo do lote se encaixa nele, mesmo com nome diferente do óbvio? NÃO \
copie um exemplo genérico nem repita sempre a mesma combinação — cada cliente tem um conjunto \
diferente de documentos. Se tem_declaracao_gratuidade for false, deixe gratuidade_documentos_total \
como 0 e gratuidade_documentos_lista como array vazio.

- gratuidade_planilha_arquivo: SOMENTE se houver, entre os documentos deste lote, uma planilha \
demonstrativa de despesas mensais (a tabela com categorias de gasto tipo moradia, alimentação, \
transporte etc. — normalmente já convertida de Excel para uma imagem .png de mesmo nome), \
informe o NOME EXATO DO ARQUIVO dessa imagem, para ser anexada como prova visual na petição \
(diferente dos outros comprovantes de gratuidade, que só são citados em texto, essa planilha É \
anexada como imagem). Se não houver essa planilha no lote, deixe "".

- gratuidade_renda_mensal / gratuidade_percentual_comprometido / gratuidade_saldo_residual: SOMENTE \
se a planilha demonstrativa de despesas mensais (a mesma de gratuidade_planilha_arquivo) estiver \
neste lote E discriminar claramente valores de renda e gastos. gratuidade_renda_mensal é a renda \
mensal total da cliente indicada na planilha, no formato "R$ X.XXX,XX". \
gratuidade_percentual_comprometido é o percentual dessa renda comprometido com a SOMA dos gastos \
essenciais (moradia, alimentação, transporte, saúde, educação, higiene — some só esses, ignore \
categorias supérfluas se a planilha tiver alguma), no formato "XX%", arredondado ao inteiro mais \
próximo. gratuidade_saldo_residual é a diferença entre a renda mensal e essa soma de gastos \
essenciais, no formato "R$ X.XXX,XX". Se a planilha não tiver essa clareza (ex: só lista gastos \
sem indicar a renda), ou não houver planilha no lote, deixe os três campos como "".

- medico_nome_crm (nome completo e CRM do médico cirurgião plástico responsável pela \
indicação/prescrição): FONTE OBRIGATÓRIA — use EXCLUSIVAMENTE o laudo médico assinado pelo \
cirurgião plástico (o documento que descreve o quadro clínico e prescreve os procedimentos \
reparadores). NÃO use para este campo comprovantes de comparecimento/atendimento, recibos de \
consulta, encaminhamentos ou qualquer outro documento administrativo — mesmo que tragam um \
nome de médico e CRM, costumam citar quem atendeu a consulta, não necessariamente quem assina \
o laudo, e a petição precisa citar o médico do laudo. Se o laudo não trouxer o nome/CRM do \
médico com clareza, deixe o campo vazio "" em vez de usar outro documento.

- procedimentos_total_no_laudo: PRIMEIRO ache, dentro do laudo médico, o trecho especifico que \
introduz a lista de CIRURGIAS (geralmente uma frase do tipo "indicada(s) a(s) cirurgia(s) \
reconstrutiva(s)/reparadora(s) de..." ou "os seguintes procedimentos cirúrgicos reparadores:", \
seguida de itens numerados 1-, 2-, 3-... ou (i), (ii), (iii)...). Conte SÓ os itens DESSA lista \
específica. NÃO conte como procedimento: cuidados pós-operatórios, curativos, cicatrização, \
malhas/meias/talas, medicamentos, anticoagulantes, fisioterapia, drenagem linfática, ou \
observações sobre agendamento/associação de cirurgias no mesmo tempo cirúrgico — mesmo que \
apareçam em frases próximas ou também numeradas, essas coisas NÃO são procedimentos cirúrgicos, \
são orientações complementares de tratamento e não entram na lista. Em especial, NUNCA trate \
como procedimento frases do tipo "tais procedimentos devem ser tratados como cirurgias \
longas...", "não recomendado sua associação no mesmo tempo cirúrgico", "coloco-me à disposição \
para esclarecimentos...", "exame presencial necessário para avaliação..." — essas são sempre \
comentários finais do médico sobre a lista, nunca um item dela; a lista de cirurgias TERMINA no \
primeiro ponto final em que o texto deixa de nomear procedimentos e passa a fazer esse tipo de \
observação geral, e nada depois desse ponto entra no array. Depois de contar os itens \
NUMERADOS (1-, 2-, 3-... ou similar), faça uma checagem extra específica: procure, no MESMO \
período/frase logo depois do último item numerado (antes do primeiro ponto final que muda de \
assunto), se existe MAIS UM nome de procedimento cirúrgico escrito ali, sem número, só separado \
por ponto e vírgula ou "e" — isso acontece com frequência (ex: "...5- BRAQUIOPLASTIA [...]; \
NINFOPLASTIA (tratamento de [...])." — aqui NINFOPLASTIA é um 6º procedimento da mesma lista, \
mesmo sem ter recebido número). Se existir, ele TAMBÉM entra na contagem e no array — é \
exatamente esse tipo de item sem número que mais fica de fora, então vale reler essa frase \
especificamente mais de uma vez antes de decidir o total. Coloque o número final aqui ANTES de \
montar procedimentos_lista, e depois confira que o array abaixo tem EXATAMENTE essa quantidade \
de elementos.

- procedimentos_lista: um ARRAY com um item de string PARA CADA procedimento da lista de \
CIRURGIAS identificada em procedimentos_total_no_laudo (mesmo critério: só as cirurgias \
daquela lista específica, nunca cuidados pós-operatórios/medicamentos/malhas/orientações de \
agendamento) — não numere manualmente (nada de "(i)", "(ii)" dentro do texto do item; a \
numeração é adicionada depois pelo sistema), cada elemento do array é só o nome/descrição do \
procedimento, literalmente como está escrito no laudo, sem resumir. Regras rígidas, porque erro \
aqui é o mais grave de todos: (1) o array TEM que ter exatamente \
procedimentos_total_no_laudo elementos — nem a mais, nem a menos (se sua resposta tiver menos, \
você parou no meio — releia o laudo até o final e complete); (2) \
NUNCA omita um procedimento só porque parece redundante ou menor (ex: "lipoaspiração de braços" \
junto de uma braquioplastia é um item À PARTE, não um detalhe da braquioplastia — inclua os \
dois como elementos separados do array se o laudo os separa); (3) NUNCA invente, complete ou \
substitua um procedimento por outro mais comum em casos parecidos (ex: não escreva "Lifting \
cervical" se o laudo não citar isso — se o laudo citar Ninfoplastia, ou qualquer outro \
procedimento incomum, transcreva exatamente esse nome); (4) use a nomenclatura EXATA do laudo \
para cada procedimento (não troque por sinônimo nem padronize para um termo "mais correto" que \
você conheça); (5) antes de finalizar, releia o laudo procedimento por procedimento, do \
primeiro ao último, e confira cada um contra o array — inclusive os últimos itens da lista do \
laudo, que são os que mais costumam ficar de fora.

- peso_perdido_kg: apenas o número + "quilos", ex: "39 quilos". FONTE OBRIGATÓRIA: use \
EXCLUSIVAMENTE o laudo do cirurgião plástico (o documento que descreve o histórico de peso da \
paciente e prescreve os procedimentos reparadores). IGNORE menções de peso/quilos perdidos em \
QUALQUER outro documento (histórico de tratamento, mensagens de WhatsApp, e-mails, termos \
administrativos etc.) — mesmo que pareçam relacionadas, podem se referir a um momento diferente \
do tratamento e não ao total atual. NÃO copie cegamente uma frase pronta do tipo "perdeu X kg" \
do laudo — procure o peso mais alto relevante (geralmente o pico do ciclo de emagrecimento mais \
recente, que motivou o tratamento atual, não picos antigos de anos anteriores) e o peso \
atual/mais recente, e CALCULE você mesmo a diferença (peso inicial − peso atual = peso \
perdido). Se o laudo já traz esse cálculo pronto, confirme que bate com os dois números \
informados; se não bater, use o resultado do seu próprio cálculo e explique em \
observacoes_divergencias. Se o laudo do cirurgião não mencionar peso, deixe o campo vazio "" \
em vez de usar outro documento.

- observacoes_divergencias: se você notar dados conflitantes entre documentos diferentes \
(ex: CEP diferente em dois documentos), anote aqui explicando a divergência e citando os \
documentos. Caso contrário, null.

CAMPOS DE LOCALIZAÇÃO DE PRINTS — a petição final inclui, como prova visual, prints (imagens) \
de trechos específicos dos documentos originais. Para cada campo abaixo, informe o NOME EXATO \
DO ARQUIVO (como aparece no rótulo "Arquivo: nome.ext" que precede cada documento) e, quando o \
arquivo for um PDF com mais de uma página, o NÚMERO DA PÁGINA dentro DAQUELE arquivo (1 = \
primeira página do arquivo, não do lote inteiro). Se o arquivo for uma imagem (png/jpg) ou um \
PDF de página única, deixe o campo de página vazio "" ou use "1". Se não conseguir identificar \
com confiança, deixe ambos os campos vazios "" — não adivinhe.

- carteirinha_frente_arquivo / carteirinha_frente_pagina: arquivo e página que mostram a FRENTE \
da carteirinha do plano de saúde (normalmente a mesma fonte de carteirinha_numero).
- carteirinha_verso_arquivo / carteirinha_verso_pagina: arquivo e página que mostram o VERSO da \
carteirinha (se houver um documento separado para o verso; caso não exista, deixe vazio).
- laudo_cirurgiao_arquivo: arquivo do laudo médico do cirurgião plástico — o MESMO documento \
usado para medico_nome_crm, procedimentos_lista e peso_perdido_kg (não confunda com atestado de \
comparecimento nem com o laudo psicológico).
- laudo_comorbidades_pagina: página, dentro de laudo_cirurgiao_arquivo, que descreve as \
comorbidades/achados físicos (a mesma informação usada em comorbidades_fisicas_1/2).
- laudo_comorbidades_trecho: um trecho LITERAL (8 a 15 palavras, cópia exata, palavra por \
palavra, do jeito que está escrito no documento — NÃO parafraseie, NÃO é a mesma redação de \
comorbidades_fisicas_1/2) do INÍCIO do parágrafo/frase daquela página que fala das comorbidades. \
O sistema usa esse trecho pra recortar visualmente só aquela parte da página (em vez da página \
inteira) — por isso precisa ser uma cópia exata que realmente exista no texto, senão o recorte \
falha e ele cai de volta pra página inteira. Se não tiver certeza de uma cópia exata, deixe "".
- laudo_procedimentos_pagina: página, dentro de laudo_cirurgiao_arquivo, que lista os \
procedimentos cirúrgicos solicitados (a mesma informação usada em procedimentos_lista — não \
precisa de um campo de trecho separado, o próprio procedimentos_lista já serve pra recortar essa \
página).
- laudo_urgencia_pagina: página, dentro de laudo_cirurgiao_arquivo, em que o médico menciona \
EXPLICITAMENTE o caráter de urgência/essencialidade dos procedimentos (palavras como "urgente", \
"vital", "fundamental", "imediato" referindo-se à necessidade do tratamento). NÃO é a mesma \
coisa que a página onde o médico simplesmente justifica os procedimentos em termos gerais (ex: \
"necessário para a qualidade de vida") — isso sozinho NÃO conta como urgência. Se o laudo não \
tiver essa menção explícita em nenhuma página, deixe vazio "" — NÃO repita o número de \
laudo_comorbidades_pagina ou laudo_procedimentos_pagina só para preencher o campo; é preferível \
deixar vazio a mostrar a mesma imagem duas vezes na petição.
- laudo_urgencia_trecho: mesma lógica de laudo_comorbidades_trecho — um trecho LITERAL (8 a 15 \
palavras, cópia exata) do início da frase/trecho daquela página que menciona a urgência, pra \
recortar só essa parte. Deixe "" se laudo_urgencia_pagina também estiver vazio.
- laudo_psicologico_arquivo: arquivo do laudo/relatório psicológico (o mesmo usado para \
psicologa_nome_crp e danos_psicologicos_paragrafo).
- laudo_psicologico_conclusao_pagina: página, dentro de laudo_psicologico_arquivo, que traz a \
CONCLUSÃO do relatório psicológico (geralmente a última página, com o resumo do quadro/danos \
psicológicos da paciente) — normalmente a mesma fonte de danos_psicologicos_paragrafo.
- laudo_psicologico_conclusao_trecho: mesma lógica de laudo_comorbidades_trecho — um trecho \
LITERAL (8 a 15 palavras, cópia exata) do início do parágrafo de conclusão daquela página, pra \
recortar só essa parte.
Responda apenas com o JSON pedido pelo schema — nada de texto fora do JSON."""

# SYSTEM_PROMPT tem ~8 mil tokens e e IDENTICO em toda chamada de lote de uma
# mesma pasta (call_batch roda 1x por lote, as vezes 2x pros lotes criticos --
# ver eh_lote_critico). Marcado com cache_control, a Anthropic reaproveita
# esse trecho entre chamadas feitas em ate 5 minutos uma da outra (a cache
# "ephemeral" padrao) cobrando soh uma fracao do preco normal de input nas
# chamadas seguintes -- economia praticamente de graca, sem nenhum efeito na
# qualidade da extracao (mesmo prompt, mesmo comportamento). Formatado como
# lista de blocos porque e assim que a API aceita marcar cache_control em
# "system"; _call_plain_json() reaproveita este MESMO objeto e so acrescenta
# um bloco extra (nao cacheado) depois, o que preserva o cache hit no trecho
# grande mesmo quando esse segundo caminho e usado.
SYSTEM_PROMPT_CACHED = [
    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
]


# ---------------------------------------------------------------------------
# Descoberta e agrupamento de arquivos
# ---------------------------------------------------------------------------

IDENTITY_DOC_KEYWORDS = {"cnh", "procuracao", "identidade", "rg"}
CARTEIRINHA_DOC_KEYWORDS = {"carteirinha", "cartao", "carteira"}
# Comprovantes de contato da cliente com a operadora -- precisam estar no
# MESMO lote entre si para o modelo reconstruir a cronologia completa dos
# fatos_lista (ex: uma solicitacao por e-mail e um reenvio por
# WhatsApp em datas diferentes so viram uma narrativa coerente se o modelo
# ve os dois numa unica chamada).
CONTATO_DOC_KEYWORDS = {
    "whatsapp", "email", "mail", "negativa", "protocolo", "solicitacao",
    "resposta", "contato", "ligacao", "ligacoes", "atendimento",
}
# Documentos que comprovam a hipossuficiencia (para gratuidade_documentos_lista
# e tem_declaracao_gratuidade) -- mesma logica: precisam estar juntos pro
# modelo listar TODOS os documentos presentes, nao so o primeiro que viu.
GRATUIDADE_DOC_KEYWORDS = {
    "pobreza", "hipossuficiencia", "ctps", "holerite", "holerites",
    "extrato", "extratos", "renda", "irpf", "declaracao",
    "planilha", "despesas", "gratuita",
    "contracheque", "contracheques", "salario", "rendimentos",
    "pagamento", "vencimentos", "irrf",
}
# O laudo do cirurgiao e o campo mais critico (procedimentos_lista) -- em
# teste real, o MESMO laudo, no MESMO modelo, produziu a lista COMPLETA (6
# de 6 procedimentos) quando estava sozinho no lote, e uma lista cortada (3
# de 6) quando dividia o lote com outros 4 documentos nao relacionados. Por
# isso esse grupo recebe lote exclusivo em make_batches() (ver
# GRUPOS_COM_LOTE_EXCLUSIVO), nunca compartilhando com "resto".
LAUDO_DOC_KEYWORDS = {"laudo", "laudos"}

GRUPOS_COM_LOTE_EXCLUSIVO = {"laudo"}


def _has_keyword(path, keywords):
    name = _normalize_text(path.stem)
    tokens = re.split(r"[^a-z0-9]+", name)
    return any(kw in tokens for kw in keywords)


def _classificar_grupos(files):
    """Separa uma lista (ja ordenada) de arquivos nos grupos tematicos acima
    -- devolve [(nome_grupo, [arquivos]), ...], preservando a ordem original
    dentro de cada grupo. Usado tanto por find_documents() (pra decidir a
    ordem) quanto por make_batches() (pra nunca dividir um grupo entre dois
    lotes, quando ele cabe inteiro num lote so, e pra isolar em lote proprio
    os grupos de GRUPOS_COM_LOTE_EXCLUSIVO)."""
    grupos_nomeados = [
        ("identidade", IDENTITY_DOC_KEYWORDS),
        ("carteirinha", CARTEIRINHA_DOC_KEYWORDS),
        ("contato", CONTATO_DOC_KEYWORDS),
        ("gratuidade", GRATUIDADE_DOC_KEYWORDS),
        ("laudo", LAUDO_DOC_KEYWORDS),
    ]
    usados = set()
    grupos = []
    for nome, palavras in grupos_nomeados:
        grupo = [f for f in files if f not in usados and _has_keyword(f, palavras)]
        usados.update(grupo)
        if grupo:
            grupos.append((nome, grupo))
    resto = [f for f in files if f not in usados]
    if resto:
        grupos.append(("resto", resto))
    return grupos


_SAIDA_ANTERIOR_FRASES = ("peticao inicial", "protocolo inicial")


def _e_saida_anterior(path):
    """True se o arquivo parece ser uma peticao/protocolo JA GERADO por este
    proprio sistema (ou pelo protocolo do processo), salvo de volta na pasta
    do cliente -- isso ja aconteceu na pratica (uma peticao inicial finalizada
    ficou salva junto com o laudo de origem) e contamina a extracao: o modelo
    pode "copiar" dali em vez de extrair dos documentos originais, inclusive
    piorando o resultado ao misturar as duas fontes. Esses arquivos nunca
    devem ir para a IA."""
    nome = _normalize_text(path.stem)
    return any(frase in nome for frase in _SAIDA_ANTERIOR_FRASES)


def _formatar_valor_celula(cell):
    """Converte o valor de uma celula do openpyxl pra texto, aplicando
    formatacao basica de moeda/porcentagem brasileira quando o formato da
    celula sugerir isso -- pra planilha convertida ficar parecida com o
    Excel, nao com numeros crus tipo 1234.5."""
    valor = cell.value
    if valor is None:
        return ""
    fmt = (cell.number_format or "").lower()
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        if "%" in fmt:
            return f"{valor * 100:.1f}%".replace(".", ",")
        if "r$" in fmt or "#,##0.00" in fmt or "0.00" in fmt:
            texto = f"{valor:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
            return f"R$ {texto}" if "r$" in fmt else texto
    return str(valor)


def _planilha_para_html(ws):
    linhas_html = []
    for row in ws.iter_rows():
        if not any(c.value is not None for c in row):
            continue
        celulas = []
        for cell in row:
            texto = html.escape(_formatar_valor_celula(cell))
            negrito = bool(cell.font and cell.font.bold)
            estilo = "font-weight:bold;" if negrito else ""
            celulas.append(f'<td style="{estilo}">{texto}</td>')
        linhas_html.append("<tr>" + "".join(celulas) + "</tr>")
    corpo = "".join(linhas_html)
    return f"<table>{corpo}</table>"


_PLANILHA_CSS = """
body{font-family:Arial,Helvetica,sans-serif; font-size:10pt;}
table{border-collapse:collapse; width:100%;}
td{border:0.75pt solid #999; padding:4px 8px; vertical-align:top;}
"""


def _planilha_para_png_bytes(xlsx_path: Path):
    """Renderiza a 1a aba da planilha como imagem (motor PyMuPDF, sem
    WeasyPrint -- mesmo motivo da geracao do PDF final, ver
    render_pdf_with_footnotes): cria uma pagina A4 paisagem e desenha a
    tabela nela. Assume que a planilha cabe numa pagina (uso real: poucas
    linhas, ex. planilha de despesas da gratuidade) -- se um dia sobrar
    conteudo, o excesso e cortado (page.insert_htmlbox devolve o texto que
    nao coube, mas nao ha pra onde mandar numa unica imagem)."""
    import openpyxl
    import pymupdf

    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
    ws = wb.worksheets[0]  # so a primeira aba -- abas extras (ex: "Como
    # preencher") sao instrucoes, nao dado da cliente, e ficam de fora.
    tabela_html = _planilha_para_html(ws)

    largura, altura = _PDF_PAGE_H_PT, _PDF_PAGE_W_PT  # A4 paisagem = A4 com W/H trocados
    margem = 10 * 72 / 25.4  # 10mm em pontos
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=largura, height=altura)
        rect = pymupdf.Rect(margem, margem, largura - margem, altura - margem)
        page.insert_htmlbox(rect, tabela_html, css=_PLANILHA_CSS)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
        return pix.tobytes("png")
    finally:
        doc.close()


def _converter_planilhas_para_imagem(folder: Path):
    """Converte cada .xlsx da pasta (recursivamente) numa imagem .png irma
    (mesmo nome, extensao .png) -- assim ela entra no pipeline normal de
    documentos (que so aceita PDF/PNG/JPG) e pode ser anexada como prova
    visual igual qualquer outro documento (ex: planilha de despesas pro
    pedido de gratuidade). Nunca lanca excecao: se uma planilha nao puder
    ser convertida, ela so fica de fora, sem travar o resto do
    processamento."""
    for xlsx_path in folder.rglob("*.xlsx"):
        png_path = xlsx_path.with_suffix(".png")
        if png_path.exists():
            continue
        try:
            png_bytes = _planilha_para_png_bytes(xlsx_path)
            if png_bytes:
                png_path.write_bytes(png_bytes)
        except Exception:
            pass


def find_documents(folder: Path):
    _converter_planilhas_para_imagem(folder)
    files = sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT and not _e_saida_anterior(p)
    )
    # Documentos de identidade, carteirinha, comprovantes de contato com a
    # operadora e comprovantes de gratuidade vao para o INICIO da lista, cada
    # grupo junto, para que o agrupamento em lotes (make_batches) tenda a
    # coloca-los no MESMO lote -- assim o modelo consegue cruzar informacao
    # entre documentos relacionados (RG/CPF/endereco entre si, numero da
    # carteirinha entre si, cronologia de contatos entre si, lista completa
    # de comprovantes de gratuidade entre si) numa unica chamada, em vez de
    # ver cada documento isolado sem nenhum outro pra comparar.
    return [f for _, grupo in _classificar_grupos(files) for f in grupo]


def make_batches(files):
    """Agrupa os arquivos (ja ordenados por find_documents) em lotes para a
    API, respeitando os limites de tamanho/quantidade e, sempre que um grupo
    tematico (ver _classificar_grupos) couber inteiro num lote, evitando
    dividir esse grupo entre dois lotes -- so assim o modelo consegue ver
    todos os documentos relacionados de uma vez.

    Grupos em GRUPOS_COM_LOTE_EXCLUSIVO (hoje so "laudo") sao tratados
    diferente: CADA ARQUIVO vira seu proprio lote, sozinho -- nem entre eles
    se combinam. Testado na pratica (mesmo laudo, mesmo modelo): sozinho no
    lote, a lista de procedimentos saiu 100% correta (6 de 6); dividindo o
    lote so com o laudo psicologico (so mais 1 arquivo, tematicamente
    relacionado), a contagem ficou certa mas o CONTEUDO dos ultimos itens
    saiu inventado. Isso e o campo mais critico da peticao, entao vale o
    custo de uma chamada de API a mais por laudo."""
    batches = []
    current, current_bytes = [], 0

    for nome, grupo in _classificar_grupos(files):
        if nome in GRUPOS_COM_LOTE_EXCLUSIVO:
            if current:
                batches.append(current)
                current, current_bytes = [], 0
            batches.extend([f] for f in grupo)
            continue
        tamanhos = [int(f.stat().st_size * 4 / 3) + 4 for f in grupo]
        bytes_grupo = sum(tamanhos)
        grupo_cabe_sozinho = len(grupo) <= MAX_FILES_PER_BATCH and bytes_grupo <= MAX_BATCH_BYTES_B64
        grupo_cabe_no_atual = (
            len(current) + len(grupo) <= MAX_FILES_PER_BATCH
            and current_bytes + bytes_grupo <= MAX_BATCH_BYTES_B64
        )
        if current and grupo_cabe_sozinho and not grupo_cabe_no_atual:
            batches.append(current)
            current, current_bytes = [], 0
        for f, tam in zip(grupo, tamanhos):
            if current and (
                len(current) >= MAX_FILES_PER_BATCH
                or current_bytes + tam > MAX_BATCH_BYTES_B64
            ):
                batches.append(current)
                current, current_bytes = [], 0
            current.append(f)
            current_bytes += tam
    if current:
        batches.append(current)
    return batches


GRUPOS_CRITICOS_DUPLA_CONSULTA = {"laudo", "gratuidade", "contato"}


def eh_lote_critico(batch):
    """True para lotes de grupos tematicos que devem ser consultados 2x (ver
    chamadas de call_batch em main() e em app.py): o laudo do cirurgiao
    (procedimentos_lista) e o lote de comprovantes de gratuidade
    (gratuidade_documentos_lista). Motivo: em teste real, a MESMA chamada
    (mesmo(s) arquivo(s), mesmo modelo) deu a lista 100% certa numa
    tentativa e incompleta/com itens trocados noutra -- e variacao do
    proprio modelo, nao um problema de lote ou de prompt. Perguntar 2x e
    deixar merge_results sinalizar divergencia (em vez de confiar cegamente
    numa unica tentativa) e a defesa possivel nesses campos, onde um item
    faltando e dificil de perceber na revisao manual."""
    if not batch:
        return False
    grupos = {nome for nome, arquivos in _classificar_grupos(batch) if arquivos}
    return bool(grupos & GRUPOS_CRITICOS_DUPLA_CONSULTA)


# ---------------------------------------------------------------------------
# PDF -> markdown (economia de tokens): a API cobra um PDF mandado como
# "document" por PAGINA, como se cada pagina fosse uma imagem -- caro para
# documentos que na verdade sao so texto (procuracao assinada eletronicamente,
# declaracao, extrato, laudo digitado, peticao/protocolo em PDF etc.). Quando
# o PDF tem uma camada de texto digital real, mandamos esse texto como
# markdown simples em vez do arquivo inteiro em base64 -- MUITO mais barato,
# e mais fiel ainda (transcricao exata, sem depender de leitura visual).
#
# So convertemos quando temos confianca razoavel de que o "texto" nao e um
# OCR ruim colado por um app de scanner em cima de uma foto de papel: cada
# pagina precisa ter uma quantidade minima de texto extraivel E nao pode ser
# dominada por uma imagem grande (sinal classico de foto/scan). RG, CNH e
# carteirinha (cartao/plano) NUNCA sao convertidos, mesmo que passem nesses
# testes -- sao sempre foto de documento fisico, e sao justamente os campos
# mais sensiveis a erro de leitura no SYSTEM_PROMPT (numero de RG, numero da
# carteirinha), entao para esse grupo o risco de um OCR de scanner errado nao
# vale a economia. Qualquer duvida (pagina sem texto suficiente, imagem
# grande demais, erro ao abrir o PDF) faz o arquivo cair no comportamento
# atual (visao), nunca no meio-termo.
PDF_MARKDOWN_TEXTO_MIN_PAGINA = 80  # caracteres minimos por pagina
PDF_MARKDOWN_COBERTURA_IMAGEM_MAX = 0.5  # fracao maxima da pagina ocupada por imagem
PDF_MARKDOWN_NUNCA_KEYWORDS = {"cnh", "identidade", "rg", "carteirinha", "cartao", "carteira"}


def _pdf_como_markdown(path: Path):
    """Devolve o conteudo do PDF em markdown se ele parecer ter texto digital
    real, ou None se deve continuar indo por visao (ver comentario acima)."""
    if _has_keyword(path, PDF_MARKDOWN_NUNCA_KEYWORDS):
        return None
    import pymupdf

    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return None
    try:
        paginas = []
        for pagina in doc:
            texto = pagina.get_text("text").strip()
            if len(texto) < PDF_MARKDOWN_TEXTO_MIN_PAGINA:
                return None
            area_pagina = pagina.rect.width * pagina.rect.height
            if area_pagina:
                area_imagens = 0.0
                for img in pagina.get_images(full=True):
                    try:
                        bbox = pagina.get_image_bbox(img)
                        area_imagens += bbox.width * bbox.height
                    except Exception:
                        area_imagens += area_pagina  # nao deu pra medir -> trata como scan, mais seguro
                if area_imagens / area_pagina > PDF_MARKDOWN_COBERTURA_IMAGEM_MAX:
                    return None
            paginas.append(texto)
        if not paginas:
            return None
        if len(paginas) == 1:
            return paginas[0]
        return "\n\n".join(f"[Página {i}]\n{t}" for i, t in enumerate(paginas, 1))
    finally:
        doc.close()


def build_content_blocks(files):
    blocks = []
    for f in files:
        media_type = SUPPORTED_EXT[f.suffix.lower()]
        markdown = _pdf_como_markdown(f) if media_type == "application/pdf" else None
        if markdown is not None:
            blocks.append({
                "type": "text",
                "text": f"Arquivo: {f.name} (PDF com texto digital, convertido para markdown)\n\n{markdown}",
            })
            continue
        data = base64.standard_b64encode(f.read_bytes()).decode("ascii")
        block_type = "document" if media_type == "application/pdf" else "image"
        blocks.append({"type": "text", "text": f"Arquivo: {f.name}"})
        blocks.append({
            "type": block_type,
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })
    blocks.append({
        "type": "text",
        "text": "Extraia os campos pedidos com base SOMENTE nos documentos acima.",
    })
    return blocks


def extract_json_loose(text):
    """Fallback: extrai o primeiro objeto JSON de um texto que pode ter markdown em volta."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Nenhum JSON encontrado na resposta.")
    return json.loads(text[start:end + 1])


def _call_plain_json(client, model, content):
    """Pede o JSON via instrucao no prompt, sem output_config. Usado quando o
    schema forcado nao e suportado (erro) OU e silenciosamente ignorado por um
    gateway (ex: OmniRoute com provedores tipo 'web' que nao aplicam o schema
    e so devolvem texto solto, sem lancar erro)."""
    response = _criar_mensagem_com_retry(
        client,
        model=model,
        max_tokens=8000,
        system=SYSTEM_PROMPT_CACHED + [
            {"type": "text", "text": "Responda APENAS com um objeto JSON válido, sem markdown, sem texto fora do JSON."},
        ],
        messages=[{"role": "user", "content": content}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return extract_json_loose(text)


TENTATIVAS_ERRO_TRANSITORIO = 3
ESPERA_ERRO_TRANSITORIO_S = 8


def _erros_transitorios_api():
    """Erros do lado do SERVIDOR da Anthropic (500 Internal Server Error,
    503/529 sobrecarga, falha de conexao) -- o SDK ja tenta de novo sozinho
    algumas vezes, mas se mesmo assim falhar, um unico lote com erro
    momentaneo nao deve derrubar o processamento inteiro da pasta (que pode
    ter 5-10 lotes). NAO inclui BadRequestError (erro do proprio pedido --
    tentar de novo do mesmo jeito nao resolve, isso e tratado a parte)."""
    import anthropic

    return (
        anthropic.InternalServerError,
        anthropic.RateLimitError,
        anthropic.OverloadedError,
        anthropic.APIConnectionError,
    )


def _criar_mensagem_com_retry(client, **kwargs):
    erros_transitorios = _erros_transitorios_api()
    for tentativa in range(1, TENTATIVAS_ERRO_TRANSITORIO + 1):
        try:
            return client.messages.create(**kwargs)
        except erros_transitorios as e:
            if tentativa == TENTATIVAS_ERRO_TRANSITORIO:
                raise
            print(
                f"     [aviso] erro temporário da API ({type(e).__name__}), "
                f"tentativa {tentativa}/{TENTATIVAS_ERRO_TRANSITORIO} — "
                f"tentando de novo em {ESPERA_ERRO_TRANSITORIO_S}s..."
            )
            time.sleep(ESPERA_ERRO_TRANSITORIO_S)


def call_batch(client, model, files, label):
    import anthropic

    print(f"  -> Lote {label}: {', '.join(f.name for f in files)}")
    content = build_content_blocks(files)
    try:
        response = _criar_mensagem_com_retry(
            client,
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT_CACHED,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        text = next(b.text for b in response.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # O servidor aceitou output_config (200 OK) mas nao aplicou o
            # schema de verdade -- comum em gateways que so repassam pra um
            # provedor sem suporte nativo a saida estruturada.
            print("     [aviso] resposta não veio em JSON puro (schema ignorado pelo provedor); tentando extrair mesmo assim.")
            try:
                return extract_json_loose(text)
            except ValueError:
                print("     [aviso] sem JSON na resposta; refazendo com instrução direta no prompt.")
                try:
                    return _call_plain_json(client, model, content)
                except ValueError:
                    preview = text.strip().replace("\n", " ")[:150]
                    print(f"     [ERRO] modelo não devolveu JSON em nenhuma tentativa para este lote. Resposta recebida: {preview!r}")
                    return {}
    except anthropic.BadRequestError as e:
        msg = str(e)
        if "output_config" in msg or "json_schema" in msg:
            print("     [aviso] output_config/json_schema não suportado neste modelo; usando modo texto simples.")
            try:
                return _call_plain_json(client, model, content)
            except _erros_transitorios_api() as e2:
                print(f"     [ERRO] falha temporária da API neste lote ({type(e2).__name__}).")
                return {}
        if len(files) > 1:
            print(f"     [aviso] lote rejeitado ({msg[:120]}...). Dividindo em lotes menores.")
            mid = len(files) // 2
            r1 = call_batch(client, model, files[:mid], label + "a")
            r2 = call_batch(client, model, files[mid:], label + "b")
            return _merge_pair(r1, r2)
        print(f"     [ERRO] não foi possível processar sozinho o arquivo {files[0].name}: {msg[:200]}")
        return {}
    except _erros_transitorios_api() as e:
        print(
            f"     [ERRO] falha temporária da API da Anthropic mesmo após "
            f"{TENTATIVAS_ERRO_TRANSITORIO} tentativas neste lote "
            f"({type(e).__name__}: {str(e)[:150]}). Os documentos deste lote "
            f"ficarão sem dados extraídos — revise manualmente os campos "
            f"relacionados a: {', '.join(f.name for f in files)}."
        )
        return {}


def _merge_pair(a, b):
    out = dict(a)
    for k, v in b.items():
        if out.get(k) in (None, "", []):
            out[k] = v
    return out


BOOLEAN_FIELDS = {"menciona_lipedema", "recebeu_negativa_formal", "tem_declaracao_gratuidade"}

# Grupos de campos que so fazem sentido vir do MESMO lote que tambem viu um
# documento-fonte especifico (a "ancora" do grupo). Sem essa checagem
# estrutural, um lote que nunca viu o documento certo pode "adivinhar" um
# valor plausivel a partir de outro papel qualquer (ex: nome de medico tirado
# de um atestado de comparecimento, nao do laudo) e vencer por acaso na hora
# de juntar os resultados dos lotes -- a ancora sempre e um campo que SO da
# pra preencher lendo o documento certo (dificil de "inventar" a partir de
# outro lugar), nunca um campo do mesmo tipo do que esta sendo ancorado.
ANCHORED_FIELD_GROUPS = [
    # so e possivel transcrever a lista numerada de procedimentos lendo o
    # laudo do cirurgiao de verdade -- ancora medico/peso/paginas de print
    # nesse mesmo lote.
    ("procedimentos_lista", {
        "peso_perdido_kg", "medico_nome_crm",
        "laudo_cirurgiao_arquivo", "laudo_comorbidades_pagina",
        "laudo_procedimentos_pagina", "laudo_urgencia_pagina",
    }),
    # so da pra escrever o paragrafo de danos psicologicos lendo o laudo
    # psicologico de verdade.
    ("danos_psicologicos_paragrafo", {
        "laudo_psicologico_arquivo", "laudo_psicologico_conclusao_pagina",
    }),
    # carteirinha_numero so vem do documento da carteirinha -- ancora os
    # campos de print (frente/verso) no mesmo lote.
    ("carteirinha_numero", {
        "carteirinha_frente_arquivo", "carteirinha_frente_pagina",
        "carteirinha_verso_arquivo", "carteirinha_verso_pagina",
    }),
    # renda/percentual/saldo so fazem sentido calculados a partir da planilha
    # de despesas de verdade -- ancora no mesmo lote que viu essa planilha,
    # senao um lote sem a planilha poderia "chutar" valores plausiveis a
    # partir de outro documento (ex: extrato bancario) sem o comparativo
    # renda x gastos essenciais que a planilha discrimina.
    ("gratuidade_planilha_arquivo", {
        "gratuidade_renda_mensal", "gratuidade_percentual_comprometido",
        "gratuidade_saldo_residual",
    }),
]

# find_documents() sempre poe os documentos de identidade (CNH, RG, Procuracao)
# no INICIO da lista, entao eles caem no primeiro lote (all_results[0]). Um
# lote sem CNH pode ver o RG em outro documento administrativo do escritorio
# (contrato, termo de ciencia -- preenchidos a partir de formulario, sujeitos
# a erro de digitacao) e reportar um valor diferente do que a CNH mostra. Da
# preferencia ao primeiro lote quando ele tiver um valor, em vez de deixar o
# primeiro resultado nao-vazio (de qualquer lote) vencer por acaso.
FIRST_BATCH_PREFERRED_FIELDS = {"cliente_rg"}


def _anchor_field_for(key):
    for anchor, dependents in ANCHORED_FIELD_GROUPS:
        if key in dependents:
            return anchor
    return None


def merge_results(all_results):
    merged = {}
    conflicts = {}
    for key in SCHEMA_PROPERTIES:
        values = []
        for r in all_results:
            v = r.get(key)
            if v not in (None, "", []):
                values.append(v)
        if key in BOOLEAN_FIELDS:
            # Um lote sem os documentos relevantes tende a responder False; isso
            # nao e um conflito de verdade — se QUALQUER lote viu evidencia (True),
            # prevalece True.
            merged[key] = any(v is True for v in values)
            continue
        anchor_field = _anchor_field_for(key)
        if anchor_field:
            anchored = [
                r.get(key) for r in all_results
                if r.get(key) not in (None, "", [])
                and r.get(anchor_field) not in (None, "", [])
            ]
            if anchored:
                uniq_anchored = list(dict.fromkeys(anchored))
                merged[key] = uniq_anchored[0]
                if len(uniq_anchored) > 1:
                    conflicts[key] = uniq_anchored
                continue
            # Nenhum lote viu o documento-ancora junto com esse campo -- cai
            # no comportamento normal abaixo (ou fica vazio, se nao houver
            # nenhum valor).
        if key in FIRST_BATCH_PREFERRED_FIELDS and all_results:
            primeiro = all_results[0].get(key)
            if primeiro not in (None, "", []):
                merged[key] = primeiro
                outros = [v for v in values if v != primeiro]
                if outros:
                    conflicts[key] = [primeiro] + list(dict.fromkeys(outros))
                continue
        if not values:
            merged[key] = None
            continue
        uniq = []
        for v in values:
            if v not in uniq:
                uniq.append(v)
        if key in ("procedimentos_lista", "gratuidade_documentos_lista", "fatos_lista") and len(uniq) > 1:
            # As duas tentativas do lote critico (ver eh_lote_critico) podem
            # divergir por variacao do proprio modelo -- entre elas, a lista
            # mais longa tem mais chance de estar completa (o erro mais
            # comum e faltar item, raramente sobrar), mas o conflito ainda
            # fica registrado abaixo pra revisao humana conferir contra os
            # documentos originais.
            merged[key] = max(uniq, key=len)
        else:
            merged[key] = uniq[0]
        if len(uniq) > 1:
            conflicts[key] = uniq
    merged["fatos_lista"] = _ordenar_fatos(merged.get("fatos_lista"))
    _drop_duplicate_urgencia_photo(merged)
    return merged, conflicts


def _data_para_iso(data):
    """Normaliza a data de um fato para AAAA-MM-DD (aceita tambem DD/MM/AAAA,
    caso o modelo devolva fora do formato pedido). Devolve "" se nao der."""
    data = (data or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", data):
        return data
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", data)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


def _ordenar_fatos(itens):
    """fatos_lista vira texto corrido em ordem cronologica. Cada item traz a
    data do proprio fato; itens sem data herdam a data do item anterior (ex:
    um complemento logo depois de um e-mail datado), para ficarem ao lado do
    fato a que se referem. Empate de data preserva a ordem original. Devolve
    a lista de strings, pronta para a petição."""
    if not itens:
        return []
    marcados = []
    data_anterior = ""
    for ordem, item in enumerate(itens):
        if isinstance(item, dict):
            data = _data_para_iso(item.get("data"))
            texto = (item.get("texto") or "").strip()
        else:
            data, texto = "", str(item or "").strip()
        data = data or data_anterior
        data_anterior = data
        if texto:
            marcados.append((data, ordem, texto))
    marcados.sort(key=lambda t: (t[0], t[1]))
    return [texto for _, _, texto in marcados]


def _drop_duplicate_urgencia_photo(merged):
    """A pagina de urgencia (laudo_urgencia_pagina) e a que mais depende de
    julgamento do modelo -- nem todo laudo menciona urgencia explicitamente.
    Se o modelo "chutou" a mesma pagina ja usada para comorbidades ou
    procedimentos (em vez de deixar vazio como instruido), a peticao
    acabaria mostrando a MESMA imagem duas vezes, o que parece erro para
    quem revisa. Melhor omitir o print de urgencia nesse caso do que
    duplicar."""
    urgencia_pagina = (merged.get("laudo_urgencia_pagina") or "").strip()
    if not urgencia_pagina:
        return
    outras_paginas = {
        (merged.get("laudo_comorbidades_pagina") or "").strip(),
        (merged.get("laudo_procedimentos_pagina") or "").strip(),
    }
    if urgencia_pagina in outras_paginas - {""}:
        merged["laudo_urgencia_pagina"] = None


# ---------------------------------------------------------------------------
# Dados cadastrais da operadora -- segunda passada focada
# ---------------------------------------------------------------------------
# Na passada principal, a operadora divide o lote com dezenas de outros
# campos (laudo, RG, gratuidade...), e o CNPJ/endereço do cartão da Receita
# Federal costumam sair vazios mesmo com o documento na pasta. Por isso, se
# algum desses campos ficou em branco, fazemos uma consulta curta, só sobre a
# operadora, percorrendo os lotes até preencher tudo -- só preenche o que
# estava vazio, nunca sobrescreve o que a passada principal já achou.

OPERADORA_CAMPOS = [
    "operadora_nome", "operadora_cnpj", "operadora_logradouro", "operadora_numero",
    "operadora_bairro", "operadora_cidade", "operadora_uf", "operadora_cep", "operadora_email",
]
OPERADORA_CAMPOS_OBRIGATORIOS = [
    "operadora_cnpj", "operadora_logradouro", "operadora_numero", "operadora_bairro",
    "operadora_cidade", "operadora_uf", "operadora_cep",
]

OPERADORA_SYSTEM_PROMPT = """Você extrai SOMENTE os dados cadastrais da OPERADORA DE PLANO DE SAÚDE \
(a empresa requerida, não a cliente) a partir dos documentos enviados, para a qualificação da \
requerida numa petição inicial. Use apenas o que está escrito nos documentos; nunca invente.

Fontes, em ordem de confiança:
1) Comprovante de inscrição e de situação cadastral no CNPJ (Receita Federal): "NOME EMPRESARIAL" \
(razão social) vai em operadora_nome, e "NÚMERO DE INSCRIÇÃO" (o CNPJ) vai em operadora_cnpj. O \
endereço vem em DUAS linhas e as duas são obrigatórias: a linha de cima traz LOGRADOURO, NÚMERO e \
COMPLEMENTO (ex: "R SANTOS DUMONT" / "2705"); a linha de baixo traz CEP, BAIRRO/DISTRITO, \
MUNICÍPIO e UF. Preencha logradouro e número com a linha de cima, e cep/bairro/cidade/uf com a \
linha de baixo. Não pare na primeira linha.
2) Consulta de operadora na ANS (Razão Social, Registro ANS, CNPJ) — confirma nome e CNPJ.
3) Carteirinha, papel timbrado de carta de negativa, rodapé de e-mail da operadora.

operadora_email é o e-mail de contato DA OPERADORA: em troca de e-mails, vem em "De:"/"From:" quando \
a operadora responde, ou em "Para:"/"To:" quando a cliente ou o escritório envia. NUNCA use o e-mail \
do escritório de advocacia nem o da cliente.

Se um número (CNPJ, CEP) estiver parcialmente ilegível, NÃO escreva asteriscos nem dígitos chutados: \
deixe o campo vazio "". Responda apenas com o JSON pedido — nada de texto fora do JSON."""

OPERADORA_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in OPERADORA_CAMPOS},
    "required": OPERADORA_CAMPOS,
    "additionalProperties": False,
}


def operadora_incompleta(data):
    return any(not (data.get(k) or "").strip() for k in OPERADORA_CAMPOS_OBRIGATORIOS)


def _call_operadora(client, model, files, label):
    print(f"  -> Consulta da operadora, lote {label}: {', '.join(f.name for f in files)}")
    content = build_content_blocks(files)
    try:
        response = _criar_mensagem_com_retry(
            client,
            model=model,
            max_tokens=2000,
            system=OPERADORA_SYSTEM_PROMPT + "\n\nResponda APENAS com um objeto JSON válido, sem markdown.",
            messages=[{"role": "user", "content": content}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        return extract_json_loose(text)
    except Exception as e:
        print(f"     [aviso] consulta da operadora falhou neste lote ({type(e).__name__}): {str(e)[:150]}")
        return {}


def completar_dados_operadora(client, model, files, data):
    """Preenche in-place os campos da operadora que a passada principal deixou
    vazios, consultando os lotes ate completar. Devolve o proprio `data`."""
    if not operadora_incompleta(data):
        return data
    print("  -> Dados da operadora incompletos; consultando de novo só essa parte...")
    for i, batch in enumerate(make_batches(files), 1):
        achado = _call_operadora(client, model, batch, str(i))
        for k in OPERADORA_CAMPOS:
            if not (data.get(k) or "").strip() and (achado.get(k) or "").strip():
                data[k] = achado[k].strip()
        if not operadora_incompleta(data):
            break
    return data


# ---------------------------------------------------------------------------
# Montagem do texto de qualificação e preenchimento do HTML
# ---------------------------------------------------------------------------

def build_qualificacao_partes(d):
    """Retorna (nome, resto) separados -- o nome precisa ficar em negrito e
    caixa alta no HTML final, igual ao padrao do escritorio, e por isso nao
    da pra montar como uma unica string de texto puro."""
    nome = (d.get("cliente_nome_completo") or "(NOME NÃO ENCONTRADO)").strip()
    partes = ["brasileira"]
    if d.get("cliente_estado_civil"):
        partes.append(d["cliente_estado_civil"])
    if d.get("cliente_profissao"):
        partes.append(d["cliente_profissao"])
    texto = ", ".join(partes)

    rg = d.get("cliente_rg") or "(RG NÃO ENCONTRADO)"
    cpf = d.get("cliente_cpf") or "(CPF NÃO ENCONTRADO)"
    texto += (
        f", portador(a) da cédula de identidade RG Nº {rg}, regularmente "
        f"inscrito(a) no C.P.F sob o Nº {cpf}"
    )

    logradouro = d.get("cliente_endereco_logradouro") or "(ENDEREÇO NÃO ENCONTRADO)"
    numero = d.get("cliente_endereco_numero") or "S/N"
    complemento = d.get("cliente_endereco_complemento")
    bairro = d.get("cliente_bairro") or "(BAIRRO NÃO ENCONTRADO)"
    cidade = d.get("cliente_cidade") or "(CIDADE NÃO ENCONTRADA)"
    uf = d.get("cliente_uf") or "(UF)"
    cep = d.get("cliente_cep") or "(CEP NÃO ENCONTRADO)"

    endereco = f"residente e domiciliado(a) na {logradouro}, Nº {numero}"
    if complemento:
        endereco += f", {complemento}"
    endereco += f", bairro {bairro}, {cidade}/{uf}, CEP: {cep}"

    return nome, texto + ", " + endereco


def data_atual_extenso():
    hoje = datetime.date.today()
    return f"{hoje.day} de {MESES_PT[hoje.month - 1]} de {hoje.year}"


def _so_primeira_maiuscula(texto):
    """Os dados da operadora (nome, logradouro, bairro, cidade) costumam vir
    em CAIXA ALTA (copiados do cartao de CNPJ), mas no corpo da peticao
    devem aparecer so com a primeira letra maiuscula -- diferente do nome
    da Requerente, que fica em negrito E caixa alta, por padrao do
    escritorio. Nao usar para UF (sigla de 2 letras, sempre maiuscula) nem
    para CNPJ/numero/CEP/e-mail."""
    texto = (texto or "").strip()
    if not texto:
        return texto
    return texto[0].upper() + texto[1:].lower()


def set_field_text(elem, text):
    node = elem
    while True:
        kids = list(node.contents)
        if len(kids) == 1 and isinstance(kids[0], Tag):
            node = kids[0]
        else:
            break
    node.string = text if text is not None else ""


# ---------------------------------------------------------------------------
# Prints de documentos embutidos na peticao (prova visual) -- cada item aqui
# vira um <img> dentro do <div class="doc-photo" data-photo="{id}"> do
# template, na pagina/arquivo que a IA indicou nos campos correspondentes.
# ---------------------------------------------------------------------------

# 5o elemento (opcional, "" quando ausente): nome do campo com um trecho
# LITERAL do documento, usado para recortar visualmente so a parte citada
# da pagina em vez da pagina inteira (ver _achar_area_dos_trechos). Vazio
# pros slots onde a pagina inteira e o proprio conteudo relevante
# (carteirinha) -- procedimentos e tratado a parte, direto em
# _insert_doc_photos, porque a "citacao" dele e a lista inteira, nao um
# campo de texto so.
PHOTO_SLOTS = [
    ("carteirinha_frente", "carteirinha_frente_arquivo", "carteirinha_frente_pagina",
     "Carteirinha do plano de saúde — frente", None),
    ("carteirinha_verso", "carteirinha_verso_arquivo", "carteirinha_verso_pagina",
     "Carteirinha do plano de saúde — verso", None),
    ("laudo_comorbidades", "laudo_cirurgiao_arquivo", "laudo_comorbidades_pagina",
     "Laudo médico — comorbidades", "laudo_comorbidades_trecho"),
    ("laudo_procedimentos", "laudo_cirurgiao_arquivo", "laudo_procedimentos_pagina",
     "Laudo médico — procedimentos solicitados", None),
    ("laudo_urgencia", "laudo_cirurgiao_arquivo", "laudo_urgencia_pagina",
     "Laudo médico — urgência", "laudo_urgencia_trecho"),
    ("laudo_psicologico_conclusao", "laudo_psicologico_arquivo", "laudo_psicologico_conclusao_pagina",
     "Laudo psicológico — conclusão", "laudo_psicologico_conclusao_trecho"),
    ("gratuidade_planilha", "gratuidade_planilha_arquivo", None,
     "Planilha demonstrativa de despesas mensais", None),
]


def _build_files_lookup(files):
    lookup = {}
    for f in files:
        lookup[_normalize_text(f.name)] = f
        lookup[_normalize_text(f.as_posix())] = f
    return lookup


def _resolve_source_file(files_lookup, arquivo_name):
    if not arquivo_name:
        return None
    key = _normalize_text(str(arquivo_name).strip())
    if key in files_lookup:
        return files_lookup[key]
    base_key = _normalize_text(Path(str(arquivo_name).strip()).name)
    return files_lookup.get(base_key)


def _achar_area_dos_trechos(pymupdf_mod, page, trechos):
    """Localiza um ou mais trechos LITERAIS de texto na pagina e devolve um
    retangulo (com margem) cobrindo so a faixa vertical onde eles aparecem
    -- a largura fica sempre a pagina inteira, pra nunca cortar o lado de
    linhas mais longas do mesmo paragrafo. Devolve None (o chamador cai de
    volta pra pagina inteira) se: nao ha trecho pra buscar, a pagina nao
    tem camada de texto pesquisavel (comum em PDF escaneado sem OCR), ou
    nenhum trecho foi encontrado."""
    if not trechos:
        return None
    if isinstance(trechos, str):
        trechos = [trechos]
    area = None
    for trecho in trechos:
        # o modelo copia o trecho do laudo, mas quebras de linha, espacos duplos
        # e hifenizacao do PDF atrapalham a busca literal -- normaliza e tenta
        # fragmentos cada vez menores ate achar algo na pagina
        trecho = re.sub(r"\s+", " ", (trecho or "")).strip()
        if len(trecho) < 8:
            continue
        rects = []
        for fragmento in (trecho, trecho[: max(8, len(trecho) // 2)], trecho[:30]):
            rects = page.search_for(fragmento)
            if rects:
                break
        for r in rects:
            area = r if area is None else area | r
    if area is None:
        return None
    margem_vertical_topo = 18
    margem_vertical_baixo = 40
    return pymupdf_mod.Rect(
        0, max(0, area.y0 - margem_vertical_topo),
        page.rect.width, min(page.rect.height, area.y1 + margem_vertical_baixo),
    )


PRINT_JPEG_QUALIDADE = 85
PRINT_IMAGEM_MAX_LADO_PX = 1600  # foto de RG/carteirinha/print cabe bem nisso e fica leve


def _imagem_arquivo_para_jpeg(path: Path) -> bytes:
    """PNG/JPG de documento -> JPEG reduzido (lado maior <= PRINT_IMAGEM_MAX_LADO_PX),
    com a rotacao do EXIF aplicada (foto tirada no celular) e transparencia
    trocada por fundo branco. Motivo: a peticao final vai no HTML que o
    navegador envia de volta pro servidor -- o limite de corpo de requisicao
    do Vercel e ~4,5 MB, e um PNG de scanner sozinho ja come boa parte disso."""
    import io
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            fundo = Image.new("RGB", im.size, (255, 255, 255))
            fundo.paste(rgba, mask=rgba.split()[-1])
            im = fundo
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((PRINT_IMAGEM_MAX_LADO_PX, PRINT_IMAGEM_MAX_LADO_PX))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=PRINT_JPEG_QUALIDADE, optimize=True)
        return buf.getvalue()


def _render_doc_photo_base64(path: Path, pagina_str, trechos_busca=None):
    """Retorna (base64, media_type, recortada) da pagina pedida (se for PDF) ou
    da propria imagem (se ja for PNG/JPG), sempre em JPEG para manter o HTML
    leve. `recortada` e True quando so a faixa do trecho foi usada (e False
    quando veio a pagina inteira). Se trechos_busca for informado, recorta so
    a faixa da pagina onde ele aparece (ver _achar_area_dos_trechos) --
    caindo de volta pra pagina inteira se nao achar. Nunca lanca excecao --
    (None, None, False) se o arquivo nao existir, a pagina nao for legivel,
    etc: o print simplesmente fica de fora da peticao em vez de travar a
    geracao."""
    try:
        ext = path.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg"):
            return base64.standard_b64encode(_imagem_arquivo_para_jpeg(path)).decode("ascii"), "image/jpeg", False
        if ext == ".pdf":
            import pymupdf
            doc = pymupdf.open(str(path))
            try:
                if pagina_str:
                    m = re.search(r"\d+", str(pagina_str))
                    pagina = int(m.group()) if m else 1
                elif doc.page_count == 1:
                    pagina = 1
                else:
                    # PDF com varias paginas e nenhuma pagina indicada -- nao
                    # da pra saber qual mostrar (ex: campo de urgencia vazio
                    # porque o laudo nao menciona isso). Sem uma pagina certa,
                    # e melhor nao inserir print nenhum do que arriscar
                    # mostrar a pagina errada ou repetir outro print.
                    return None, None, False
                idx = max(0, min(pagina - 1, doc.page_count - 1))
                page = doc.load_page(idx)
                clip = _achar_area_dos_trechos(pymupdf, page, trechos_busca)
                if clip is not None:
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=clip)
                else:
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
                jpeg = pix.tobytes("jpg", jpg_quality=PRINT_JPEG_QUALIDADE)
                return base64.standard_b64encode(jpeg).decode("ascii"), "image/jpeg", clip is not None
            finally:
                doc.close()
    except Exception:
        pass
    return None, None, False


def _insert_one_photo(soup, files_lookup, slot_id, arquivo_valor, pagina_valor, caption,
                      trechos_busca=None, paginas_inteiras_usadas=None):
    """Preenche (ou remove, se nao houver imagem) um <div class="doc-photo">
    -- devolve True se preencheu, False se removeu (sem documento, ou pagina
    inteira que ja apareceu em outro slot). paginas_inteiras_usadas registra
    as paginas mostradas sem recorte: sem isso, quando o trecho nao acha o
    texto, o laudo inteiro entrava na peticao uma vez por slot."""
    div = soup.find("div", attrs={"data-photo": slot_id})
    if div is None:
        return None
    path = _resolve_source_file(files_lookup, arquivo_valor)
    b64, media_type, recortada = (None, None, False)
    if path is not None:
        b64, media_type, recortada = _render_doc_photo_base64(path, pagina_valor, trechos_busca)
    if not b64:
        div.decompose()
        return False
    if paginas_inteiras_usadas is not None and not recortada:
        chave = (path.as_posix(), re.sub(r"\D", "", str(pagina_valor or "")) or "1")
        if chave in paginas_inteiras_usadas:
            div.decompose()
            return False
        paginas_inteiras_usadas.add(chave)
    img = soup.new_tag("img")
    img["src"] = f"data:{media_type};base64,{b64}"
    img["alt"] = caption
    div.clear()
    div.append(img)
    cap = soup.new_tag("p")
    cap["class"] = "doc-photo-caption"
    cap.string = caption
    div.append(cap)
    return True


def _insert_doc_photos(soup, data, files):
    filled, skipped = [], []
    files_lookup = _build_files_lookup(files) if files else {}
    paginas_inteiras_usadas = set()
    procedimentos = data.get("procedimentos_lista") or []
    for slot_id, arquivo_field, pagina_field, caption, trecho_field in PHOTO_SLOTS:
        if slot_id == "laudo_procedimentos":
            # a "citacao" aqui e a lista inteira, nao um campo de texto so --
            # busca pelo primeiro E o ultimo procedimento e recorta a faixa
            # que cobre os dois, capturando a lista completa mesmo quando
            # ela e longa (em vez de uma margem fixa que podia cortar o fim).
            trechos = [procedimentos[0], procedimentos[-1]] if procedimentos else None
        else:
            trechos = data.get(trecho_field) if trecho_field else None
        resultado = _insert_one_photo(
            soup, files_lookup, slot_id, data.get(arquivo_field), data.get(pagina_field), caption, trechos,
            paginas_inteiras_usadas=paginas_inteiras_usadas,
        )
        if resultado is None:
            continue
        (filled if resultado else skipped).append(slot_id)

    return filled, skipped


# ---------------------------------------------------------------------------
# Geracao do PDF final -- motor proprio em PyMuPDF, sem WeasyPrint. Antes
# usava CSS Paged Media avancado (float:footnote, position:running()) que
# so o WeasyPrint suporta bem, e que por sua vez depende de bibliotecas
# nativas de sistema (Pango/Cairo) indisponiveis em hospedagem serverless
# (Vercel). Aqui a mesma logica -- nota de rodape real na pagina exata da
# citacao, com timbrado repetindo em toda pagina -- e reimplementada em
# Python puro sobre PyMuPDF (pymupdf.Story), que ja e dependencia do
# projeto e nao precisa de nada alem do proprio pacote pip.
#
# Como o motor HTML do PyMuPDF nao tem um algoritmo de nota de rodape
# embutido (nenhum, alem do WeasyPrint/Prince, tem), a posicao de cada
# nota e calculada aqui: o corpo e "fluido" pagina a pagina reservando uma
# altura de rodape por pagina, comeca em zero e vai ajustando por
# convergencia (poucas iteracoes bastam, ja que o numero de notas costuma
# ser pequeno) ate a altura reservada bater com o que as notas realmente
# citadas naquela pagina precisam.
# ---------------------------------------------------------------------------
_PDF_PAGE_W_PT = 210 * 72 / 25.4  # largura A4 em pontos
_PDF_PAGE_H_PT = 297 * 72 / 25.4  # altura A4 em pontos
_PDF_SIDE_MARGIN_PT = 65 * 72 / 96  # padding lateral de main.petition (65px CSS) em pontos

_PDF_BODY_CSS = """
main{padding:0; line-height:1.15; font-size:13pt; font-family:Cambria,'Times New Roman',Georgia,serif; text-align:justify;}
/* Igual ao .cond.hide do editor (ver <style> do modelo): sem esta regra, o
   motor do PDF ignora a classe "hide" que o JS do editor aplica nos blocos
   condicionais desmarcados no painel, e desenha as duas variantes de cada
   grupo (ex: gratuidade de adulto E de menor, Do Direito padrao E de
   autogestao) -- petição com seções I) e II) repetidas e contraditórias. */
.cond.hide{display:none;}
main p{margin:0 0 12px 0; text-indent:4.75cm;}
main p.noindent{text-indent:0;}
h2.title{text-align:center; font-size:13pt; line-height:1.15; margin:26px 0; text-indent:0; font-weight:bold;}
h3.sec{font-size:13pt; line-height:1.15; text-indent:0; margin:26px 0 14px 0; font-weight:bold;}
h4.subsec{font-size:13pt; line-height:1.15; text-indent:0; margin:20px 0 12px 0; font-weight:bold;}
.cond-badge{font-family:Arial,Helvetica,sans-serif; font-size:9pt; color:#7a2432;}
blockquote.quote{margin:0; margin-left:5cm; font-size:11pt; line-height:1.15; text-indent:0; color:#222;}
blockquote.quote cite{display:block; margin-top:6px; font-style:normal; font-size:11pt;}
sup a{color:#2b5fa3; text-decoration:none; font-size:0.75em;}
.doc-photo{margin:10px 0 18px 0; text-align:center;}
.doc-photo img{max-width:60%; height:auto; border:1px solid #bbb;}
.doc-photo-caption{font-family:Arial,Helvetica,sans-serif; font-size:9pt; color:#666; margin:4px 0 0 0; text-indent:0;}
table.docrow{width:100%; border-collapse:collapse; margin:10px 0 18px 0;}
table.docrow td{text-align:center; vertical-align:top; padding:0 6px;}
table.docrow img{max-width:100%; height:auto; border:1px solid #bbb;}
"""

_PDF_FOOTNOTE_CSS = """
div.fn{font-size:9.5pt; color:#444; margin:0 0 4px 0; text-indent:0; font-family:Cambria,'Times New Roman',Georgia,serif;}
div.fn a{color:#2b5fa3;}
"""

_PDF_MAX_PAGINAS = 300  # trava de seguranca contra loop infinito em HTML malformado
_PDF_MAX_ITERACOES_RODAPE = 4


def _pdf_img_size(data_uri):
    """(largura, altura) em px de uma imagem data:URI, ou None se falhar."""
    try:
        import io
        from PIL import Image
        raw = base64.b64decode(data_uri.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as im:
            return im.size
    except Exception:
        return None


def _pdf_agrupar_doc_photos_em_tabela(soup):
    """Converte cada <div class="doc-photos-row"> (que no navegador usa
    display:flex, sem suporte no motor HTML do PyMuPDF) numa <table>
    equivalente, uma coluna por doc-photo, pra manter as imagens lado a
    lado no PDF em vez de empilhadas."""
    for row in soup.find_all("div", class_="doc-photos-row"):
        fotos = [f for f in row.find_all("div", class_="doc-photo", recursive=False) if f.find("img")]
        if not fotos:
            row.decompose()
            continue
        table = soup.new_tag("table")
        table["class"] = "docrow"
        tr = soup.new_tag("tr")
        largura = f"{100 // len(fotos)}%"
        for foto in fotos:
            td = soup.new_tag("td")
            td["style"] = f"width:{largura};"
            td.append(foto.extract())
            tr.append(td)
        table.append(tr)
        row.replace_with(table)


def _pdf_flow_body(body_html, nota_id_por_marca, alturas_rodape_por_pagina, header_h, footer_h):
    """Flui o corpo (pymupdf.Story) pagina a pagina, entre o cabecalho e o
    rodape (header_h/footer_h) e reservando em cada pagina a altura de nota
    de rodape informada em alturas_rodape_por_pagina (paginas sem entrada
    usam 0). Devolve (rects, citas_por_pagina): os retangulos de conteudo
    usados em cada pagina, e um dict pagina->set de ids de nota cujas
    citacoes calharam naquela pagina."""
    import io
    import pymupdf

    story = pymupdf.Story(html=body_html, user_css=_PDF_BODY_CSS)
    citas_por_pagina = {}

    def registrar(pos):
        # open_close: 1/2 = abre/fecha (elementos de bloco, cada um vira um
        # evento separado); 3 = elemento inline reportado de uma vez so (e
        # o caso do <sup> da citacao) -- aceitar qualquer valor e simples e
        # seguro aqui porque so estamos ADICIONANDO a um set (duplicata na
        # mesma pagina nao muda nada).
        if pos.id in nota_id_por_marca:
            citas_por_pagina.setdefault(pos.page_num, set()).add(nota_id_por_marca[pos.id])

    # place() so calcula a proposta de layout -- e draw() que "confirma" o
    # conteudo daquela pagina e avanca o fluxo pra proxima; sem chamar
    # draw() aqui (mesmo sem precisar do pixel final), place() ficaria
    # sempre devolvendo a mesma pagina. Desenha num DocumentWriter
    # descartavel so por isso -- cabecalho/rodape/notas ficam de fora
    # dessa passada de medicao, so entram na passada final (ja com a
    # altura de rodape convergida).
    buf_descartavel = io.BytesIO()
    writer_descartavel = pymupdf.DocumentWriter(buf_descartavel)
    mediabox = pymupdf.Rect(0, 0, _PDF_PAGE_W_PT, _PDF_PAGE_H_PT)

    rects = []
    page_num = 0
    more = 1
    while more:
        base = _PDF_PAGE_H_PT - footer_h - alturas_rodape_por_pagina.get(page_num, 0.0)
        rect = pymupdf.Rect(_PDF_SIDE_MARGIN_PT, header_h, _PDF_PAGE_W_PT - _PDF_SIDE_MARGIN_PT, base)
        dev = writer_descartavel.begin_page(mediabox)
        more, _ = story.place(rect)
        story.draw(dev)
        writer_descartavel.end_page()
        story.element_positions(registrar, {"page_num": page_num})
        rects.append(rect)
        page_num += 1
        if page_num > _PDF_MAX_PAGINAS:
            raise RuntimeError("Petição com número anormal de páginas -- abortando geração de PDF.")
    writer_descartavel.close()
    return rects, citas_por_pagina


def _pdf_altura_notas(notes_html, ids_notas, largura):
    """Altura (pt) necessaria pra caber o texto das notas dadas na largura
    informada, medida de verdade com Story.fit_height (nao e um chute)."""
    import pymupdf

    if not ids_notas:
        return 0.0
    html = "".join(notes_html[nid] for nid in ids_notas)
    story = pymupdf.Story(html=html, user_css=_PDF_FOOTNOTE_CSS)
    resultado = story.fit_height(largura, height_min=5, height_max=_PDF_PAGE_H_PT)
    return resultado.rect.height + 6  # respiro entre o corpo e a nota


def render_pdf_with_footnotes(html_content: str) -> bytes:
    """Gera o PDF final a partir do HTML ja preenchido/editado (recebido tal
    como esta na tela, com os campos preenchidos e as condicoes marcadas),
    com as referencias como notas de rodape reais na pagina onde a citacao
    caiu, e o timbrado (cabecalho/rodape) repetindo em toda pagina."""
    import io
    import pymupdf

    soup = BeautifulSoup(html_content, "html.parser")
    toolbar = soup.find("div", class_="toolbar")
    if toolbar is not None:
        toolbar.decompose()

    thead_img = soup.select_one("thead img")
    tfoot_img = soup.select_one("tfoot img")
    header_src = thead_img["src"] if thead_img and thead_img.get("src") else None
    footer_src = tfoot_img["src"] if tfoot_img and tfoot_img.get("src") else None
    header_size = _pdf_img_size(header_src) if header_src else None
    footer_size = _pdf_img_size(footer_src) if footer_src else None
    header_h = _PDF_PAGE_W_PT * (header_size[1] / header_size[0]) if header_size else 0.0
    footer_h = _PDF_PAGE_W_PT * (footer_size[1] / footer_size[0]) if footer_size else 0.0

    # notas de rodape: guarda o texto de cada <p class="fn" id="notaN"> (ja
    # vem com o "N. " no inicio, escrito no proprio modelo) e tira o bloco
    # de endnotes do corpo -- cada nota e desenhada a parte, na pagina certa.
    notes_html = {}
    endnotes = soup.find("div", class_="endnotes")
    if endnotes is not None:
        for p in endnotes.find_all("p", class_="fn", id=True):
            notes_html[p["id"]] = f'<div class="fn">{p.decode_contents()}</div>'
        endnotes.decompose()

    # marca cada citacao com um id rastreavel, pra saber em qual pagina ela
    # caiu depois de fluir o corpo (ver _pdf_flow_body/element_positions)
    nota_id_por_marca = {}
    for i, a in enumerate(soup.find_all("a", href=re.compile(r"^#nota\d+$"))):
        nota_id = a["href"].lstrip("#")
        if nota_id not in notes_html:
            continue
        marca = f"__cite_{nota_id}_{i}"
        (a.find_parent("sup") or a)["id"] = marca
        nota_id_por_marca[marca] = nota_id

    main = soup.find("main", class_="petition") or soup.find("main")
    if main is None:
        raise ValueError('Modelo sem <main class="petition"> -- não é possível gerar o PDF.')
    _pdf_agrupar_doc_photos_em_tabela(main)
    body_html = str(main)

    largura_conteudo = _PDF_PAGE_W_PT - 2 * _PDF_SIDE_MARGIN_PT
    alturas = {}
    for _ in range(_PDF_MAX_ITERACOES_RODAPE):
        rects, citas_por_pagina = _pdf_flow_body(body_html, nota_id_por_marca, alturas, header_h, footer_h)
        novas_alturas = {
            pn: _pdf_altura_notas(notes_html, sorted(notas), largura_conteudo)
            for pn, notas in citas_por_pagina.items()
        }
        if novas_alturas == alturas:
            break
        alturas = novas_alturas
    # rects/citas_por_pagina refletem a ultima iteracao (convergida, ou a
    # ultima tentativa se o limite de iteracoes foi atingido -- na pratica,
    # com poucas notas por documento, converge nas 2 primeiras)

    body_story = pymupdf.Story(html=body_html, user_css=_PDF_BODY_CSS)
    _IMG_CSS = "img{width:100%; display:block;}"

    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    mediabox = pymupdf.Rect(0, 0, _PDF_PAGE_W_PT, _PDF_PAGE_H_PT)
    for page_num, rect in enumerate(rects):
        dev = writer.begin_page(mediabox)
        # cabecalho/rodape sao uma Story NOVA a cada pagina -- assim como o
        # corpo, uma Story "consome" o que ja foi desenhado a cada
        # place()+draw(); reaproveitar a mesma instancia faria a imagem
        # aparecer só na primeira pagina e sumir nas seguintes.
        if header_src:
            header_story = pymupdf.Story(html=f'<img src="{header_src}">', user_css=_IMG_CSS)
            header_story.place(pymupdf.Rect(0, 0, _PDF_PAGE_W_PT, header_h))
            header_story.draw(dev)
        if footer_src:
            footer_story = pymupdf.Story(html=f'<img src="{footer_src}">', user_css=_IMG_CSS)
            footer_story.place(pymupdf.Rect(0, _PDF_PAGE_H_PT - footer_h, _PDF_PAGE_W_PT, _PDF_PAGE_H_PT))
            footer_story.draw(dev)
        body_story.place(rect)
        body_story.draw(dev)
        notas_pagina = sorted(citas_por_pagina.get(page_num, ()))
        if notas_pagina:
            nota_html = "".join(notes_html[nid] for nid in notas_pagina)
            nota_story = pymupdf.Story(html=nota_html, user_css=_PDF_FOOTNOTE_CSS)
            nota_rect = pymupdf.Rect(
                _PDF_SIDE_MARGIN_PT, rect.y1, _PDF_PAGE_W_PT - _PDF_SIDE_MARGIN_PT, _PDF_PAGE_H_PT - footer_h
            )
            nota_story.place(nota_rect)
            nota_story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


# Quando um campo nao e encontrado, o span NAO fica intocado com o texto de
# exemplo do modelo (isso ja causou petição real com "REGIONAL DA LAPA" --
# um foro plausivel mas errado, porque o campo tinha ficado sem preencher e
# ninguem percebeu que aquilo era so o texto de exemplo do modelo). Em vez
# disso, o span vira um placeholder visivelmente "em branco" (mesmo estilo
# tracejado/cinza dos campos .blank do modelo), com uma dica do que fazer.
_BLANK_HINTS = {
    "comarca_foro": (
        "[FORO A DEFINIR — cliente na Capital/SP: consulte o CEP em "
        "https://www.tjsp.jus.br/app/CompetenciaTerritorial]"
    ),
    "gratuidade_documentos": "[listar os documentos juntados para a gratuidade]",
    "gratuidade_renda_mensal": "[renda mensal — ver planilha de despesas]",
    "gratuidade_percentual_comprometido": "[% comprometido — ver planilha de despesas]",
    "gratuidade_saldo_residual": "[saldo residual — ver planilha de despesas]",
    "cliente_idade": "[idade — ver RG/CNH/certidão de nascimento]",
}
_BLANK_HINT_PADRAO = "[dado não encontrado nos documentos — preencher manualmente]"

_NUMERAIS_ROMANOS = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"]


def _formatar_procedimentos(lista):
    """procedimentos_lista vem da IA como array (um item por procedimento,
    sem numeracao) -- aqui vira o texto corrido numerado em algarismos
    romanos que o modelo de peticao usa, ex: "(i) X; (ii) Y; (iii) Z."."""
    if not lista:
        return None
    itens = []
    for idx, item in enumerate(lista):
        item = (item or "").strip()
        if not item:
            continue
        numeral = _NUMERAIS_ROMANOS[idx] if idx < len(_NUMERAIS_ROMANOS) else str(idx + 1)
        itens.append(f"({numeral}) {item}")
    if not itens:
        return None
    return "; ".join(itens) + "."


def _insert_fatos_lista(soup, itens):
    """Insere a narrativa cronologica dos fatos como uma sequencia dinamica
    de paragrafos dentro do container [data-field-list="fatos_lista"] --
    quantos itens fatos_lista tiver, tantos paragrafos aparecem (sem o teto
    fixo de 3 paragrafos que existia antes e forcava resumir o historico).
    Devolve True se preencheu, False se removeu o container (sem fatos)."""
    container = soup.select_one('[data-field-list="fatos_lista"]')
    if container is None:
        return False
    itens = [t.strip() for t in (itens or []) if t and t.strip()]
    if not itens:
        container.decompose()
        return False
    container.clear()
    for texto in itens:
        p = soup.new_tag("p")
        p["class"] = "fill"
        p["contenteditable"] = "true"
        p.string = texto
        container.append(p)
    return True


def _formatar_lista_documentos(lista):
    """gratuidade_documentos_lista vem da IA como array (um tipo de \
documento por item) -- aqui vira prosa corrida em portugues, ex: \
"declaração de pobreza, CTPS e holerite"."""
    itens = [t.strip() for t in (lista or []) if t and t.strip()]
    if not itens:
        return None
    if len(itens) == 1:
        return itens[0]
    return ", ".join(itens[:-1]) + " e " + itens[-1]


def fill_template(template_path: Path, output_path: Path, data: dict, files=None):
    with open(template_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    field_values = {
        "comarca_foro": resolve_comarca_foro(data),
        "email_requerente": data.get("cliente_email"),
        "cliente_idade": data.get("cliente_idade"),
        "requerida_nome": _so_primeira_maiuscula(data.get("operadora_nome")),
        "requerida_cnpj": data.get("operadora_cnpj"),
        "requerida_logradouro": _so_primeira_maiuscula(data.get("operadora_logradouro")),
        "requerida_numero": data.get("operadora_numero"),
        "requerida_bairro": _so_primeira_maiuscula(data.get("operadora_bairro")),
        "requerida_cidade": _so_primeira_maiuscula(data.get("operadora_cidade")),
        "requerida_uf": data.get("operadora_uf"),
        "requerida_cep": data.get("operadora_cep"),
        "requerida_email": data.get("operadora_email"),
        "carteirinha_numero": data.get("carteirinha_numero"),
        "peso_perdido": data.get("peso_perdido_kg"),
        "comorbidades_1": data.get("comorbidades_fisicas_1"),
        "comorbidades_2": data.get("comorbidades_fisicas_2"),
        "comorbidades_psicologicas": data.get("comorbidades_psicologicas"),
        "lipedema_1": data.get("lipedema_paragrafo_1"),
        "lipedema_2": data.get("lipedema_paragrafo_2"),
        "medico_nome_crm": data.get("medico_nome_crm"),
        "procedimentos": _formatar_procedimentos(data.get("procedimentos_lista")),
        "psicologa_nome_crp": data.get("psicologa_nome_crp"),
        "danos_psicologicos": data.get("danos_psicologicos_paragrafo"),
        "danos_morais_laudo_1": data.get("danos_morais_laudo_1"),
        "danos_morais_laudo_2": data.get("danos_morais_laudo_2"),
        "danos_morais_relacao": data.get("danos_morais_relacao"),
        "gratuidade_documentos": _formatar_lista_documentos(data.get("gratuidade_documentos_lista")),
        "gratuidade_renda_mensal": data.get("gratuidade_renda_mensal"),
        "gratuidade_percentual_comprometido": data.get("gratuidade_percentual_comprometido"),
        "gratuidade_saldo_residual": data.get("gratuidade_saldo_residual"),
        "data_atual": data_atual_extenso(),
    }

    filled, skipped = [], []

    nome_cliente_raw = data.get("cliente_nome_completo")
    nome, resto_qualificacao = build_qualificacao_partes(data)
    qual_spans = soup.select('[data-field="qualificacao_requerente"]')
    if not nome_cliente_raw:
        skipped.append("qualificacao_requerente")
    else:
        for span in qual_spans:
            span.clear()
            strong = soup.new_tag("strong")
            strong.string = f"{nome.upper()} (Requerente),"
            span.append(strong)
            span.append(" " + resto_qualificacao)
        filled.append("qualificacao_requerente")

    for field_name, value in field_values.items():
        spans = soup.select(f'[data-field="{field_name}"]')
        if not value:
            skipped.append(field_name)
            hint = _BLANK_HINTS.get(field_name, _BLANK_HINT_PADRAO)
            for span in spans:
                span["class"] = ["blank"]
                set_field_text(span, hint)
            continue
        for span in spans:
            if "blank" in span.get("class", []):
                span["class"] = ["fill"]
            set_field_text(span, value)
        filled.append(field_name)

    checkbox_map = {
        "cond-gratuidade": data.get("tem_declaracao_gratuidade"),
        "cond-negativa": (
            (not data["recebeu_negativa_formal"])
            if data.get("recebeu_negativa_formal") is not None else None
        ),
        "cond-lipedema": data.get("menciona_lipedema"),
    }
    for cb_id, checked in checkbox_map.items():
        if checked is None:
            continue
        cb = soup.find("input", id=cb_id)
        if cb is None:
            continue
        if checked:
            cb["checked"] = ""
        elif cb.has_attr("checked"):
            del cb["checked"]

    title_tag = soup.find("title")
    nome_cliente = data.get("cliente_nome_completo")
    if title_tag and nome_cliente:
        title_tag.string = f"Petição Inicial - {nome_cliente}"

    photo_filled, photo_skipped = _insert_doc_photos(soup, data, files)
    filled += photo_filled
    skipped += photo_skipped

    # cond-planilha so fica marcado se a foto da planilha realmente entrou
    # (nao so porque a IA disse um nome de arquivo) -- assim o paragrafo
    # "a Requerente junta planilha demonstrativa..." nunca aparece sem a
    # imagem correspondente de fato anexada.
    cb_planilha = soup.find("input", id="cond-planilha")
    if cb_planilha is not None:
        if "gratuidade_planilha" in photo_filled:
            cb_planilha["checked"] = ""
        elif cb_planilha.has_attr("checked"):
            del cb_planilha["checked"]

    fatos = _ordenar_fatos(data.get("fatos_lista"))
    (filled if _insert_fatos_lista(soup, fatos) else skipped).append("fatos_lista")

    output_path.write_text(str(soup), encoding="utf-8")
    return filled, skipped


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def main():
    parser = argparse.ArgumentParser(description="Preenche a petição inicial a partir da pasta de documentos de um cliente.")
    parser.add_argument("pasta", help="Caminho da pasta com os documentos do cliente")
    parser.add_argument("--output", help="Caminho do HTML de saída (padrão: dentro da pasta do cliente)")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="Caminho do modelo HTML base")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Modelo da API (padrão: {DEFAULT_MODEL})")
    parser.add_argument("--dry-run", action="store_true", help="Só mostra os lotes que seriam enviados, sem chamar a API")
    args = parser.parse_args()

    folder = Path(args.pasta).resolve()
    if not folder.is_dir():
        print(f"Pasta não encontrada: {folder}")
        sys.exit(1)

    template_path = Path(args.template).resolve()
    if not template_path.is_file():
        print(f"Modelo não encontrado: {template_path}")
        sys.exit(1)

    files = find_documents(folder)
    if not files:
        print(f"Nenhum PDF/PNG/JPG encontrado em {folder}")
        sys.exit(1)

    print(f"Encontrados {len(files)} documentos em {folder}:")
    for f in files:
        marca = ""
        if f.suffix.lower() == ".pdf" and _pdf_como_markdown(f) is not None:
            marca = "  [vai como markdown — texto digital, não como imagem]"
        print(f"  - {f.relative_to(folder)} ({f.stat().st_size / 1024:.0f} KB){marca}")

    batches = make_batches(files)
    print(f"\nSerão enviados {len(batches)} lote(s) para o modelo {args.model}:")
    for i, batch in enumerate(batches, 1):
        total_kb = sum(f.stat().st_size for f in batch) / 1024
        print(f"  Lote {i}: {len(batch)} arquivo(s), ~{total_kb:.0f} KB -> {[f.name for f in batch]}")

    if args.dry_run:
        print("\n(--dry-run: nenhuma chamada de API foi feita)")
        return

    import anthropic
    try:
        client = anthropic.Anthropic()
    except Exception as e:
        print(f"Erro ao criar cliente da API: {e}")
        sys.exit(1)

    print()
    all_results = []
    for i, batch in enumerate(batches, 1):
        all_results.append(call_batch(client, args.model, batch, str(i)))
        if eh_lote_critico(batch):
            all_results.append(call_batch(client, args.model, batch, f"{i}b"))

    data, conflicts = merge_results(all_results)
    completar_dados_operadora(client, args.model, files, data)

    output_path = Path(args.output).resolve() if args.output else folder / f"Petição Inicial - {folder.name}.html"
    output_path = unique_output_path(output_path)

    filled, skipped = fill_template(template_path, output_path, data, files=files)

    print("\n" + "=" * 70)
    print(f"Petição gerada em: {output_path}")
    print("=" * 70)
    print(f"\nCampos preenchidos ({len(filled)}): {', '.join(filled)}")
    if skipped:
        print(f"\nCampos NÃO encontrados nos documentos ({len(skipped)}) — revisar manualmente:")
        for s in skipped:
            print(f"  - {s}")
    if conflicts:
        print(f"\n⚠ DIVERGÊNCIAS entre documentos — confirme manualmente:")
        for k, vals in conflicts.items():
            print(f"  - {k}: {vals}")
    if data.get("observacoes_divergencias"):
        print(f"\nObservações da IA: {data['observacoes_divergencias']}")
    print(
        "\nLembrete: Comarca/Foro e nº da Vara NÃO são preenchidos automaticamente "
        "(dependem de decisão jurídica) — ajuste manualmente no arquivo gerado."
    )
    print("Abra o HTML no navegador, revise os campos amarelos e os checkboxes do topo antes de imprimir/protocolar.")


if __name__ == "__main__":
    main()
