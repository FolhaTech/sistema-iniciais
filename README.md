# Sistema Iniciais

Sistema web que lê os documentos de um cliente (RG, CNH, laudos, comprovantes
etc.), usa a API da Anthropic (Claude) para extrair os dados e preenche
automaticamente uma petição inicial a partir de um modelo HTML.

O resultado é sempre um ponto de partida — campos não encontrados e
divergências entre documentos ficam sinalizados no próprio HTML gerado para
revisão do advogado antes de protocolar.

## Uso local (Windows)

Veja [COMO_USAR.txt](COMO_USAR.txt) — passo a passo completo (chave da API,
como rodar, como gerar o PDF final).

## Publicar na internet

Veja [DEPLOY.md](DEPLOY.md) — deploy no Render via Docker, com login
protegendo o acesso.

## Stack

Flask + Anthropic API (Claude) + PyMuPDF (leitura de PDF/imagem) +
WeasyPrint (geração de PDF) + BeautifulSoup (preenchimento do template HTML).
