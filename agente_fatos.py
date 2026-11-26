"""Agente de FATOS: escreve a narrativa cronológica da seção "DOS FATOS".

Usa o mesmo cliente/chave do Gemini do restante do sistema, mas numa chamada
própria, só para os fatos: lê SOMENTE os documentos numerados da pasta do
cliente ("1 Solicitacao via email", "2 Retorno app", ..., "7 Decisao de
junta") na ordem do número, e conta a história da solicitação da cliente à
operadora até o que levou a ação (negativa expressa ou tácita).

O resultado substitui `fatos_lista` dos dados extraídos; o texto entra na
petição logo depois do parágrafo "Além disso, o laudo menciona expressamente
a realização dos procedimentos reparadores em caráter de urgência...".
Sem arquivos numerados (ou se o agente falhar), nada muda: segue valendo o
fatos_lista da extração geral.
"""

import re

_NUMERADO = re.compile(r"^\s*0*(\d{1,3})(?![\d])\s*[-_.)]?\s*\S")

SYSTEM_PROMPT = """\
Você é um assistente jurídico do escritório Arantes Arimura, especializado em ações contra \
operadoras de plano de saúde (cirurgias reparadoras pós-bariátricas). Sua ÚNICA tarefa é redigir \
a narrativa dos FATOS da petição inicial: a história cronológica da solicitação da cliente \
(Requerente) à operadora (Requerida), desde o pedido de autorização dos procedimentos até o que \
levou ao ajuizamento da ação — a NEGATIVA, que pode ser EXPRESSA (a operadora nega os \
procedimentos) ou TÁCITA (a operadora não dá retorno informando autorização ou negativa).

FONTE: os documentos recebidos estão NUMERADOS na ordem em que os fatos aconteceram (o número \
está no início do nome do arquivo, que vem antes do conteúdo). Use SOMENTE esses documentos. \
Não use conhecimento externo, não presuma nada que os documentos não mostrem.

PRECISÃO (regra máxima):
- Cada afirmação deve ter base num documento. Nunca invente datas, canais, protocolos, nomes de \
atendentes, motivos de negativa ou valores.
- Datas: copie a data EXATA do documento (cabeçalho do e-mail, data da mensagem de WhatsApp, data \
da carta/decisão). Escreva por extenso no texto ("em 10/06/2026"). Se o documento não trouxer data, \
não chute: use "data" vazia e não cite data no texto.
- Protocolos: copie cada número EXATAMENTE como está no documento, sem arredondar nem completar, e \
nunca atribua o protocolo de um documento a outro fato.
- Motivos e normas da negativa: cite os motivos e as diretrizes/normas EXATAMENTE como o documento \
da operadora informa, procedimento por procedimento quando houver mais de um.
- Se dois documentos se contradizem, siga o documento mais específico e registre a divergência em \
"observacoes".

ESCOPO: somente a relação entre a Requerente e a Requerida (operadora): pedidos, reenvios, ligações, \
respostas, protocolos, junta médica, decisão, negativa, falta de resposta. Se algum documento for \
conversa entre a cliente e o próprio escritório de advocacia, IGNORE-o. NÃO descreva quadro clínico, \
comorbidades nem os laudos (isso vai em outras partes da petição) e NÃO faça argumentação jurídica: \
apenas narre o que aconteceu.

ESTILO: terceira pessoa, linguagem formal de petição, pretérito perfeito, feminino ("a Requerente", \
"a Requerida"). Cada fato ou contato distinto é o SEU PRÓPRIO parágrafo (item do array), em ordem \
cronológica; não comprima fatos diferentes num parágrafo só e não repita o mesmo fato duas vezes. \
Estilo de referência: "Com a referida prescrição médica, a Requerente entrou em contato com a \
Requerida, via e-mail, em 10/06/2026, solicitando autorização para a realização dos procedimentos \
[...]". Escreva como se a própria Requerente tivesse feito o contato, a menos que o documento mostre \
expressamente que foi outra pessoa. Informe o canal (e-mail, WhatsApp, aplicativo, telefone) e o \
que foi pedido/respondido, com o máximo de detalhe que o documento permitir.

NEGATIVA: o último(s) parágrafo(s) devem narrar o desfecho. Se houve resposta formal negando, \
narre a negativa (data, canal, motivos, normas citadas). Se NÃO houve resposta formal da operadora \
sobre autorização ou negativa, narre a ausência de resposta (negativa tácita), indicando a data do \
último contato e que a operadora permaneceu inerte. Se a operadora autorizou algum procedimento, \
narre também. A negativa nunca vem antes do pedido que ela responde.

Responda APENAS com JSON válido, sem markdown:
{"fatos_lista":[{"data":"AAAA-MM-DD","texto":"..."}],
 "tipo_negativa":"expressa"|"tacita"|"nao_identificada",
 "documentos_usados":["nome do arquivo", ...],
 "observacoes":"divergências ou lacunas encontradas, ou \\"\\""}
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "fatos_lista": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"data": {"type": "string"}, "texto": {"type": "string"}},
                "required": ["data", "texto"],
                "additionalProperties": False,
            },
        },
        "tipo_negativa": {"type": "string", "enum": ["expressa", "tacita", "nao_identificada"]},
        "documentos_usados": {"type": "array", "items": {"type": "string"}},
        "observacoes": {"type": "string"},
    },
    "required": ["fatos_lista", "tipo_negativa", "documentos_usados", "observacoes"],
    "additionalProperties": False,
}


def arquivos_numerados(files):
    """Arquivos cujo NOME começa com número ("1 Solicitacao...", "02 - ..."),
    ordenados pelo número (empate: pelo nome). Pasta/dados pessoais sem número
    ficam de fora de propósito."""
    achados = []
    for f in files:
        m = _NUMERADO.match(f.name)
        if m:
            achados.append((int(m.group(1)), f.name.lower(), f))
    achados.sort(key=lambda t: (t[0], t[1]))
    return [f for _, _, f in achados]


def gerar_fatos(client, model, files):
    """Roda o agente. Devolve (resultado, motivo): resultado é o dict do agente
    ({"fatos_lista": [...], ...}) ou None se não há documentos numerados / a
    chamada falhou, e nesse caso motivo explica por quê (aparece na petição)."""
    import preencher_peticao as pp

    numerados = arquivos_numerados(files)
    if not numerados:
        print("  -> Agente de fatos: nenhum documento numerado encontrado; mantendo os fatos da extração geral.")
        return None, "nenhum arquivo com nome começando por número foi encontrado na pasta; os fatos vieram da extração geral"
    print(f"  -> Agente de fatos: {', '.join(f.name for f in numerados)}")

    content = pp.build_content_blocks(numerados)
    content[-1] = {
        "type": "text",
        "text": (
            "Os documentos acima estão em ordem numérica = ordem cronológica dos fatos. "
            "Redija a narrativa dos fatos com base SOMENTE neles, seguindo as regras."
        ),
    }
    try:
        response = pp._criar_mensagem_com_retry(
            client,
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        texto = next(b.text for b in response.content if b.type == "text")
        resultado = pp.extract_json_loose(texto)
    except Exception as e:
        print(f"     [aviso] agente de fatos falhou ({type(e).__name__}): {str(e)[:200]}")
        return None, f"falhou ({type(e).__name__}: {str(e)[:150]}); os fatos vieram da extração geral"

    itens = [
        {"data": (i.get("data") or "").strip(), "texto": (i.get("texto") or "").strip()}
        for i in resultado.get("fatos_lista") or []
        if isinstance(i, dict) and (i.get("texto") or "").strip()
    ]
    if not itens:
        print("     [aviso] agente de fatos não devolveu nenhum fato; mantendo os da extração geral.")
        return None, "não devolveu nenhum fato; os fatos vieram da extração geral"
    resultado["fatos_lista"] = itens
    return resultado, None


def aplicar_agente_fatos(client, model, files, data):
    """Substitui data["fatos_lista"] pelo resultado do agente (in-place).
    Devolve `data`; as observações do agente vão para observacoes_divergencias."""
    resultado, motivo = gerar_fatos(client, model, files)
    if not resultado:
        _anotar(data, "[Agente de fatos NÃO aplicado] " + motivo)
        return data
    _anotar(data, "[Agente de fatos aplicado] documentos usados: " + ", ".join(resultado.get("documentos_usados") or []))
    data["fatos_lista"] = resultado["fatos_lista"]
    tipo = resultado.get("tipo_negativa")
    if tipo == "tacita":
        data["recebeu_negativa_formal"] = False
    elif tipo == "expressa":
        data["recebeu_negativa_formal"] = True
    obs = (resultado.get("observacoes") or "").strip()
    if obs:
        _anotar(data, "[Fatos] " + obs)
    return data


def _anotar(data, texto):
    atual = (data.get("observacoes_divergencias") or "").strip()
    data["observacoes_divergencias"] = (atual + "\n" if atual else "") + texto
