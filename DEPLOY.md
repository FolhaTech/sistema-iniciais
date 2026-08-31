# Publicar o sistema na internet (Render)

O app foi feito para rodar como um processo Flask tradicional (upload de
pasta → processamento que pode levar minutos → geração de PDF com
`weasyprint`, que precisa de bibliotecas nativas de sistema). Isso não
encaixa bem no modelo do Vercel, que é voltado para funções serverless de
execução curta e sem sistema de arquivos persistente. Por isso o deploy
aqui é para o [Render](https://render.com), que roda o `Dockerfile` deste
repositório como um serviço web comum, sem esse tipo de limitação.

O repositório já tem tudo pronto (`Dockerfile`, `render.yaml`,
`.gitignore` protegendo dados de clientes e segredos). Faltam só os passos
abaixo, que só podem ser feitos por quem tem acesso à conta:

## 1. Criar conta / logar no Render
https://dashboard.render.com — pode entrar direto com a conta do GitHub.

## 2. Criar o serviço a partir do Blueprint
1. No painel, clique em **New +** → **Blueprint**.
2. Selecione o repositório GitHub deste projeto (autorize o Render a
   acessar sua conta do GitHub se pedir).
3. O Render lê o `render.yaml` e propõe o serviço `sistema-iniciais`
   automaticamente. Confirme.

## 3. Preencher os segredos
O Render vai pedir para preencher as variáveis marcadas como secretas no
`render.yaml` (elas nunca ficam no código):

- `ANTHROPIC_API_KEY` — a mesma chave usada localmente
  (console.anthropic.com/settings/keys).
- `APP_USERNAME` / `APP_PASSWORD` — o login que vai proteger o app público.
  **Escolha uma senha forte** — sem isso, qualquer pessoa com o link
  poderia usar o sistema com documentos de terceiros e gastar sua chave da
  Anthropic.
- `SHAREPOINT_*` (opcional) — só preencha se for usar a integração com
  SharePoint; sem elas, o upload manual de pasta continua funcionando
  normalmente.

`SECRET_KEY` é gerada automaticamente pelo Render (não precisa mexer).

## 4. Deploy
Clique em **Apply** / **Create**. O primeiro build demora alguns minutos
(instala as dependências do sistema do weasyprint). Ao terminar, o Render
mostra a URL pública (algo como `https://sistema-iniciais.onrender.com`).

## 5. Testar
Abra a URL, entre com o `APP_USERNAME`/`APP_PASSWORD` configurados, e rode
um processamento de teste com uma pasta pequena antes de usar com um caso
real.

## Limitações importantes a observar

- **Plano gratuito "dorme"**: sem uso por 15 minutos, o app hiberna; a
  próxima requisição demora ~50s pra acordar, e isso soma com o tempo de
  processamento normal (que já pode levar minutos) — na prática, o
  primeiro processamento depois de um tempo parado pode estourar o tempo
  limite. Se isso acontecer na prática, troque `plan: free` para
  `plan: starter` no `render.yaml` (é pago, mas fica sempre ativo) e
  faça um novo commit — o Render reaplica o blueprint automaticamente.
- **Requisição síncrona longa**: o processamento roda direto na
  requisição HTTP (sem fila em segundo plano). O `gunicorn` está
  configurado com `--timeout 600` (10 min), mas alguns provedores também
  aplicam um limite próprio no proxy de borda — se pastas muito grandes
  derem timeout mesmo assim, me avise: dá pra mover o processamento para
  segundo plano (ex: com polling), mas é uma mudança de arquitetura maior
  que não valia a pena adiantar sem confirmar que é necessário.
- **Disco efêmero**: qualquer arquivo salvo dentro do container (uploads
  temporários, petições geradas) some a cada novo deploy/reinício. Isso já
  é esperado para os uploads temporários (sempre apagados após o
  processamento), mas significa que as **petições geradas não ficam
  guardadas no servidor** — baixe/salve cada petição (botão "Gerar PDF" ou
  Ctrl+S) assim que for gerada, ela não vai continuar disponível depois.
- **Custo da API**: como o app fica acessível na internet (ainda que atrás
  de login), monitore o uso em console.anthropic.com — cada processamento
  de pasta consome a chave configurada no servidor.
