# Publicar o sistema na internet (Vercel)

O repositório já tem tudo pronto para o Vercel: o Flask (`app.py`) é
detectado automaticamente (zero-config), o `vercel.json` define o tempo
máximo de execução, e a geração de PDF usa PyMuPDF em vez do WeasyPrint —
sem bibliotecas nativas de sistema, o que funciona no ambiente serverless
do Vercel (o WeasyPrint não funcionaria lá; essa foi a mudança de código
feita especificamente para viabilizar esse deploy).

Faltam só os passos abaixo, que só podem ser feitos por quem tem acesso à
conta:

## 1. Criar conta / logar no Vercel
https://vercel.com — pode entrar direto com a conta do GitHub.

## 2. Importar o repositório
1. No painel, clique em **Add New...** → **Project**.
2. Selecione o repositório `FolhaTech/sistema-iniciais` (autorize o
   Vercel a acessar sua conta do GitHub se pedir).
3. O Vercel detecta o Flask automaticamente (por causa do `app.py` na raiz
   e do `vercel.json`) — não precisa mudar nenhuma configuração de build.

## 3. Preencher as variáveis de ambiente
Antes de clicar em Deploy, na seção **Environment Variables**, adicione:

- `ANTHROPIC_API_KEY` — a mesma chave usada localmente
  (console.anthropic.com/settings/keys).
- `APP_USERNAME` / `APP_PASSWORD` — o login que vai proteger o app
  público. **Escolha uma senha forte** — sem isso, qualquer pessoa com o
  link poderia usar o sistema com documentos de terceiros e gastar sua
  chave da Anthropic.
- `SECRET_KEY` — uma string aleatória longa (gere uma com
  `python -c "import secrets; print(secrets.token_hex(32))"` no seu
  computador e cole o resultado).
- `APP_ENV` = `production`.
- `SHAREPOINT_*` (opcional) — só preencha se for usar a integração com
  SharePoint; sem elas, o upload manual de pasta continua funcionando
  normalmente.

Essas variáveis nunca ficam no código nem no `vercel.json` — só existem
dentro do painel do Vercel.

## 4. Deploy
Clique em **Deploy**. Em 1-2 minutos o Vercel mostra a URL pública (algo
como `https://sistema-iniciais.vercel.app`).

## 5. Testar
Abra a URL, entre com o `APP_USERNAME`/`APP_PASSWORD` configurados, e rode
um processamento de teste com uma pasta pequena antes de usar com um caso
real -- inclusive o botão "Gerar PDF", pra confirmar que a nota de rodapé
está caindo na página certa (a lógica foi reimplementada do zero pra essa
migração; veja "O que mudou" abaixo).

### Alternativa via linha de comando
Se preferir não usar o painel: `npx vercel login` (abre o navegador pra
autenticar) e depois `npx vercel --prod` dentro da pasta do projeto. As
variáveis de ambiente do passo 3 ainda precisam ser configuradas (`npx
vercel env add NOME_DA_VARIAVEL`) antes do deploy funcionar de verdade.

## Limitações importantes a observar

- **Tempo de execução**: o `vercel.json` está com `maxDuration: 300`
  (5 minutos, o teto do plano Hobby). Se pastas grandes de cliente (muitos
  documentos, vários lotes) derem timeout, e você estiver no plano Pro,
  pode subir esse valor para até 800 (ou 1800 em beta) editando
  `vercel.json` e commitando de novo.
- **Disco efêmero**: nada gravado no servidor persiste entre requisições
  (é assim que o Vercel funciona). Isso já é esperado pros uploads
  temporários (sempre apagados após o processamento), mas significa que as
  **petições geradas não ficam guardadas no servidor** — baixe/salve cada
  petição (botão "Gerar PDF" ou Ctrl+S) assim que for gerada.
- **Custo da API**: como o app fica acessível na internet (ainda que atrás
  de login), monitore o uso em console.anthropic.com — cada processamento
  de pasta consome a chave configurada no servidor.

## O que mudou no código pra viabilizar isso

O sistema usava **WeasyPrint** pra gerar o PDF final (a versão com as
referências como notas de rodapé reais, na página certa). O WeasyPrint
depende de bibliotecas nativas de sistema (Pango/Cairo) que o ambiente
serverless do Vercel não fornece — é um problema conhecido e sem solução
simples nesse tipo de hospedagem.

A geração de PDF foi reescrita em Python puro sobre o **PyMuPDF**
(`pymupdf.Story`), que já era dependência do projeto e não tem nenhuma
dependência nativa externa. A lógica de posicionar a nota de rodapé na
página exata da citação — antes feita pelo motor de CSS avançado do
WeasyPrint (`float:footnote`, `position:running()`) — foi refeita à mão:
o corpo do texto é "fluído" página a página e a posição de cada citação é
rastreada, com a altura reservada pra nota ajustada por algumas iterações
até convergir. Testado localmente (inclusive dentro de um container Docker
com disco somente-leitura, simulando o ambiente do Vercel) com um
documento de várias páginas, múltiplas notas na mesma página e imagens de
documentos lado a lado — tudo caiu no lugar certo. Ainda assim, **confira
o PDF gerado num caso real antes de confiar cegamente** (passo 5 acima),
como qualquer mudança desse tamanho merece.

A mesma troca foi feita na conversão de planilha (.xlsx) pra imagem, que
também usava WeasyPrint por baixo.

## Alternativa: Render (Docker)

O repositório também tem um `Dockerfile` e `render.yaml` funcionais, caso
prefira uma hospedagem mais parecida com um servidor tradicional (sem os
limites de tempo de execução do serverless) em vez do Vercel. Peça pra eu
detalhar esse caminho se decidir usá-lo.
