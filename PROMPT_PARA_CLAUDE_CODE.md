# Prompt para o Claude Code (colar dentro da pasta `auto-follow-instagram`)

---

Você vai construir, **nesta pasta**, um automatizador para o Instagram web que percorre
um grupo de DM, segue quem curtiu cada post compartilhado e marca o post como feito com
uma reação de coração. Leia primeiro o `API_REFERENCE.md` desta pasta — ele foi extraído
de uma captura real (Fiddler) do fluxo manual e é a fonte da verdade dos endpoints,
headers e payloads.

## O fluxo (exatamente, na ordem)

1. **Login / sessão**: usar uma sessão já logada do Instagram (reaproveitar cookies do
   navegador — NÃO fazer login programático com usuário/senha, isso dispara checkpoint).
2. Abrir o grupo de DM `vai toma no quase nada` — `thread_id = 24092553240433373`
   (`https://www.instagram.com/direct/t/24092553240433373/`). O chat tem vários posts
   compartilhados (mensagens do tipo `MESSAGE_INLINE_SHARE`).
3. **Achar o ponto de retomada**: percorrer os posts do chat e localizar o **último post
   que JÁ tem a minha reação de ❤️**. A reação serve de marcador "já processado".
4. O **próximo post sem ❤️** (logo depois do último com ❤️) é o que será processado.
5. Abrir esse post → ler a **lista de curtidas** (likers).
6. **Seguir todas as pessoas que curtiram** o post (uma a uma), pulando quem eu já sigo
   ou já tenho pedido pendente.
7. Ao terminar a lista de curtidas, **sair do post**, voltar ao chat e **reagir ❤️** na
   mensagem daquele post (marca como feito).
8. Seguir para o **próximo post** e repetir 5–7, até acabar os posts ou bater o limite
   diário configurado.

## Arquitetura recomendada (decida, mas justifique)

Recomendação: **Playwright reaproveitando a sessão logada** (o projeto opensquad já tem
`@playwright/mcp` e perfis salvos em `_opensquad/_browser_profile/`). Motivos: o fluxo é
visual (achar o último post com coração), reaproveitar a sessão evita re-login/checkpoint,
e é muito mais resistente à rotação de tokens do que replicar o GraphQL cru.

Híbrido sugerido:
- **UI (Playwright)** para: navegar no chat, detectar o coração em cada post (ler o badge
  de reação), abrir o post, e aplicar a reação ❤️.
- **API interna via contexto da página** (`page.request` / `fetch` de dentro da página
  logada, que já carrega cookies + `csrftoken` automaticamente) para: paginar os
  **likers** e executar os **follows** — é mais robusto e te dá `friendship_status` para
  pular quem já sigo. Endpoints e payloads exatos no `API_REFERENCE.md`.
- Para obter o `media_id` do post: ao abrir o post pela UI a URL vira `/p/CODE/`; converta
  `CODE → media_id` com o helper base64 do `API_REFERENCE.md` (ou leia o id da página).

Se preferir abordagem 100% API (sem navegador), tudo bem, mas então trate: extração de
`fb_dtsg`/`lsd`/`__hs`/`__rev`/`__spin_*` do HTML para o GraphQL da reação, rotação de
`X-IG-WWW-Claim`, e o mapeamento post→`media_id` (os `xma` muitas vezes vêm como
"Mensagem indisponível").

## Regras de SEGURANÇA que DEVEM estar no código (não são opcionais)

Isto aqui é o que evita o ban — implemente de verdade, com valores em config:

- **Cap diário de follows** (config, começar baixo: `MAX_FOLLOWS_DIA = 60`).
- **Cap por hora** (`MAX_FOLLOWS_HORA = 15`) e cap por post.
- **Delays aleatórios humanos**: 25–70 s entre follows; 3–8 min entre posts; pausas
  longas ocasionais. Nada de intervalo fixo.
- **Janela de execução**: só rodar em horário "humano" (ex.: 09h–23h), nunca 24/7.
- **Pular**: quem eu já sigo (`friendship_status.following`), pedidos pendentes
  (`outgoing_request`), e opcionalmente contas privadas (config `SEGUIR_PRIVADOS=false`).
- **Kill-switch de bloqueio**: se qualquer resposta vier `feedback_required`,
  `checkpoint_required`, `spam`, `status != ok`, HTTP 400/429 ou texto "Ação bloqueada",
  **parar tudo imediatamente** e gravar timestamp de cooldown (não voltar por 24–48 h).
- **Estado persistente** (`output/state.json`): posts já processados, usuários já
  seguidos (set), contadores do dia, último cursor — para **retomar** sem refazer e sem
  estourar limite. A reação ❤️ no chat já é um marcador, mas mantenha o estado local
  também como rede de segurança.
- **Modo `--dry-run`**: percorre e loga tudo, sem seguir nem reagir.
- **Logs** em `output/` com timestamp de cada ação.

## Convenções do projeto (obrigatório)

- Nada de arquivos gerados na raiz: tudo em `output/` (logs, state, screenshots), criando
  a pasta se não existir.
- Config no topo / `config.py` ou `.env` — nada hardcoded de credencial.
- Português nos textos de log e README.
- Entregar: o(s) script(s), um `requirements.txt`/`package.json`, e um `README.md`
  explicando como configurar a sessão, rodar em `--dry-run` e os parâmetros de segurança.

## Entregáveis

1. Script principal do fluxo (Playwright + API interna conforme acima).
2. `config` com todos os caps/delays/janela.
3. `output/` com state + logs.
4. `README.md` com setup, `--dry-run`, e um aviso de risco de bloqueio/ban no topo.
5. Ao final, rode em `--dry-run` e me mostre o log da detecção (qual seria o "próximo
   post sem ❤️" e quantos curtidores seriam seguidos) **antes** de qualquer ação real.
