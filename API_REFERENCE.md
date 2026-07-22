# Referência de API — fluxo "auto-follow / follow" no chat do Instagram

> Extraído de uma captura Fiddler (`quaseseguenada.saz`, 573 sessões) do fluxo real
> executado manualmente no `instagram.com` (web). Use como fonte da verdade do que
> cada ação dispara. **Nenhum segredo de sessão está aqui** — `csrftoken`, `sessionid`,
> `fb_dtsg`, `lsd`, `X-IG-WWW-Claim` etc. devem ser lidos da sessão logada em runtime.

## Constantes

| Item | Valor |
|------|-------|
| Grupo (DM) | `vai toma no quase nada` |
| `thread_id` | `24092553240433373` |
| URL da thread | `https://www.instagram.com/direct/t/24092553240433373/` |
| `X-IG-App-ID` | `936619743392459` (app id web padrão) |
| `X-ASBD-ID` | `359341` |
| Base URL | `https://www.instagram.com` |

## Sequência real observada (por post)

```
useCDSWebLoginMutation                         # login (1x na sessão)
IGDMessageListOffMsysQuery   (paginado 10x)    # lê mensagens da thread (scroll)
─── para cada post a processar ───
GET /api/v1/media/{media_id}/info/             # (opcional) metadados do post
GET /api/v1/media/{media_id}/likers/           # lista de quem curtiu
POST /api/v1/friendships/create/{user_id}/     # segue cada curtidor (N chamadas)
IGDirectReactionSendMutation                   # reage ❤️ na mensagem do post (marca "feito")
─── próximo post ───
```

## 1) Listar mensagens da thread — `IGDMessageListOffMsysQuery`

- `POST https://www.instagram.com/api/graphql`
- Form: `fb_api_req_friendly_name=IGDMessageListOffMsysQuery`, `doc_id=26407294142279455`,
  `variables={...thread/cursor...}`, + boilerplate (`fb_dtsg`, `lsd`, `jazoest`, `__a=1`, `av`, `__hs`, `__rev`, `__spin_*`, `__dyn`, `__csr` ...).
- Resposta (zstd): `data.fetch__SlideThread.as_ig_direct_thread.slide_messages.edges[]`
  - `node.message_id`  → ex.: `mid.$gAFWYDK2g0t2kMcVlr2eAIAc44UbA`  (use na reação)
  - `node.content_type` → `MESSAGE_INLINE_SHARE` = post compartilhado
  - `node.reactions`   → reações da mensagem (vazio = ainda não processado)
  - `node.content.xma` → payload do post; quando hidratado traz o permalink/shortcode.
    ⚠️ Na captura muitos vinham como placeholder "Mensagem indisponível" (posts
    privados/excluídos) — **não confie só nisso para extrair o shortcode**; o caminho
    robusto é abrir o post pela UI e ler a URL `/p/CODE/`.
- Paginação: usa `cursor` dos `edges` para carregar histórico mais antigo.

## 2) Curtidores do post — likers

- `GET https://www.instagram.com/api/v1/media/{media_id}/likers/`
- Headers: `X-IG-App-ID: 936619743392459`, `X-ASBD-ID: 359341`, `X-CSRFToken: <csrftoken>`,
  `X-IG-WWW-Claim: <claim>`, `X-Requested-With: XMLHttpRequest`, `Referer: https://www.instagram.com/p/<CODE>/`, Cookies da sessão.
- Resposta JSON:
```json
{ "users": [
  { "pk": "76232099735", "username": "juliatilco", "full_name": "júlia tilço",
    "is_private": true, "is_verified": false,
    "friendship_status": { "following": false, "outgoing_request": false,
                           "followed_by": false, "is_private": true } },
  ...
] }
```
- **`friendship_status.following`** = já segue → pular. **`outgoing_request`** = pedido
  pendente → pular. `is_private` = seguir gera *pedido* (pendente), não follow imediato.

## 3) Seguir — `usePolarisFollowMutation` (GraphQL)

> ⚠️ O endpoint REST legado `POST /api/v1/friendships/create/{id}/` **não funciona
> mais** a partir da origem web: o IG responde com 302 → shell HTML (HTTP 200 + página).
> Confirmado por captura ao vivo (`diag_capturar_follow.py`): o instagram.com moderno
> segue por uma **mutation GraphQL**. Use esta.

- `POST https://www.instagram.com/api/graphql`
- `Content-Type: application/x-www-form-urlencoded`
- Form: `fb_api_req_friendly_name=usePolarisFollowMutation`, `doc_id=26508036048874888`,
  + boilerplate (`fb_dtsg`, `lsd`, `jazoest`, `__a=1`, `av`, `__comet_req=7`, ...),
  + `variables` (URL-encoded):
```json
{ "target_user_id": "5550610199",
  "container_module": "profile",
  "nav_chain": "" }
```
- Headers: `X-FB-Friendly-Name: usePolarisFollowMutation`, `X-CSRFToken`, `X-ASBD-ID`,
  `X-IG-App-ID`, cookies da sessão.
- Resposta:
```json
{ "data": { "xdt_create_friendship": {
    "username": "...", "id": "5550610199",
    "friendship_status": { "following": true, "outgoing_request": false,
                           "followed_by": false, "is_bestie": false } } },
  "extensions": { "is_final": true } }
```
  → privado vira `following:false, outgoing_request:true` (pedido pendente).
- Resposta de bloqueio (parar imediatamente): `errors[]` com `feedback_required`/
  `checkpoint`/`spam`/`blocked`, ou HTTP 400/429.

## 4) Reagir ❤️ na mensagem — `IGDirectReactionSendMutation`

- `POST https://www.instagram.com/api/graphql`
- Form: `fb_api_req_friendly_name=IGDirectReactionSendMutation`, `doc_id=24374451552236906`,
  + boilerplate, + `variables` (URL-encoded):
```json
{ "input": {
    "emoji": "❤",
    "item_id": "",
    "message_id": "mid.$gAFWYDK2g0t2jEDy1Fmc3vdK9MFXX",
    "reaction_status": "created",
    "thread_id": "24092553240433373"
} }
```
- ⚠️ `❤` é o U+2764 (HEAVY BLACK HEART), não o emoji vermelho U+2764+FE0F.
- Para tirar a reação: `reaction_status: "deleted"`.

## Conversão pk ↔ shortcode (base64 do Instagram) — confirmada na captura

```python
ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
def pk_to_code(pk):
    pk = int(pk); s = ""
    while pk > 0:
        s = ALPHA[pk & 63] + s; pk >>= 6
    return s
def code_to_pk(code):
    pk = 0
    for ch in code: pk = pk * 64 + ALPHA.index(ch)
    return pk
# 3747865033981397111 <-> "DQDF2QwkYh3"  (validado)
```
