"""
Cliente do Instagram web.

Estratégia: dirigir um Chrome logado via Playwright e fazer TODAS as chamadas de
API de dentro do contexto da própria página logada (fetch same-origin). Assim os
cookies, fingerprint e headers são os do navegador real — e não precisamos
reimplantar login nem assinar requests.

Endpoints/payloads vêm da captura real (ver API_REFERENCE.md).
"""
import json
import re
import random

from playwright.sync_api import sync_playwright

import config
from safety import log, checar_bloqueio, BloqueioDetectado, ErroTransitorio, explicar_status

# doc_ids capturados
DOC_MESSAGE_LIST = "26407294142279455"   # IGDMessageListOffMsysQuery
DOC_REACTION = "24374451552236906"       # IGDirectReactionSendMutation
DOC_FOLLOW = "26508036048874888"         # usePolarisFollowMutation (follow real do web)


# ───────────── JS injetado na página logada ─────────────
JS_TOKENS = r"""
() => {
  const html = document.documentElement.innerHTML;
  const pick = (re) => { const m = html.match(re); return m ? m[1] : null; };
  const cookie = (n) => {
    const m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]+)'));
    return m ? decodeURIComponent(m[1]) : null;
  };
  const dtsg = pick(/"DTSGInitialData",\[\],\{"token":"([^"]+)"/)
            || pick(/"dtsg":\{"token":"([^"]+)"/)
            || pick(/name="fb_dtsg" value="([^"]+)"/);
  const lsd = pick(/"LSD",\[\],\{"token":"([^"]+)"/)
            || pick(/"lsd":\{"token":"([^"]+)"/);
  const av = pick(/"actorID":"(\d+)"/)
          || pick(/"IG_USER_EIMU":"(\d+)"/)
          || pick(/"viewerId":"(\d+)"/)
          || cookie('ds_user_id');
  let claim = '0';
  try { claim = sessionStorage.getItem('www-claim-v2') || '0'; } catch (e) {}
  return { dtsg, lsd, av, claim, csrf: cookie('csrftoken'),
           dsuser: cookie('ds_user_id') };
}
"""

JS_API_GET = r"""
async (p) => {
  const r = await fetch(p.url, { credentials: 'include', headers: {
    'x-ig-app-id': p.appid, 'x-asbd-id': p.asbd, 'x-csrftoken': p.csrf,
    'x-requested-with': 'XMLHttpRequest', 'x-ig-www-claim': p.claim,
  }});
  return { status: r.status, text: await r.text() };
}
"""

JS_GRAPHQL = r"""
async (p) => {
  const body = new URLSearchParams();
  body.set('av', p.av);
  body.set('__a', '1');
  body.set('__comet_req', '7');
  body.set('dpr', '1');
  body.set('fb_dtsg', p.dtsg);
  body.set('jazoest', p.jazoest);
  body.set('lsd', p.lsd);
  body.set('fb_api_caller_class', 'RelayModern');
  body.set('fb_api_req_friendly_name', p.friendly);
  body.set('server_timestamps', 'true');
  body.set('doc_id', p.doc_id);
  body.set('variables', p.variables);
  const r = await fetch('/api/graphql', {
    method: 'POST', credentials: 'include', headers: {
      'content-type': 'application/x-www-form-urlencoded',
      'x-fb-friendly-name': p.friendly, 'x-csrftoken': p.csrf,
      'x-asbd-id': p.asbd, 'x-ig-app-id': p.appid,
    }, body: body.toString() });
  return { status: r.status, text: await r.text() };
}
"""


def _jazoest(dtsg: str) -> str:
    """Algoritmo clássico do Facebook: '2' + soma dos charCodes do fb_dtsg."""
    if not dtsg:
        return ""
    return "2" + str(sum(ord(c) for c in dtsg))


def _parse_json(text: str):
    """IG às vezes prefixa a resposta com 'for (;;);'."""
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    return json.loads(text)


# ─────────── conversão pk <-> shortcode (base64 do IG) ───────────
_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def code_to_pk(code: str) -> int:
    pk = 0
    for ch in code:
        pk = pk * 64 + _ALPHA.index(ch)
    return pk


def pk_to_code(pk) -> str:
    pk = int(pk)
    s = ""
    while pk > 0:
        s = _ALPHA[pk & 63] + s
        pk >>= 6
    return s


def _fetch_transitorio(msg) -> bool:
    """True se o erro do page.evaluate é uma FALHA DE REDE naquele fetch (recuperável:
    pula o follow e segue). NÃO inclui browser fechado/crash (aí a run não continua)."""
    m = (msg or "").lower()
    if any(k in m for k in ("closed", "crash", "target page", "detached", "has been closed")):
        return False
    return any(k in m for k in (
        "failed to fetch", "networkerror", "load failed", "net::err",
        "err_network", "err_internet", "err_connection", "err_timed_out", "err_name_not_resolved"))


class IG:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._pw = None
        self.ctx = None
        self.page = None
        self.tokens = {}

    # ───────────────── ciclo de vida ─────────────────
    def abrir(self):
        self._pw = sync_playwright().start()
        kwargs = dict(
            headless=config.HEADLESS,
            locale=config.LOCALE,
            user_agent=config.USER_AGENT,
            viewport={"width": 1280, "height": 820},
            args=["--disable-blink-features=AutomationControlled"],
            # tira a tarja "controlado por software automatizado" e o sinal de bot
            # (ajuda o reCAPTCHA do login a funcionar)
            ignore_default_args=["--enable-automation"],
        )
        if getattr(config, "PROXY", None):
            kwargs["proxy"] = config.PROXY
            log.info("🌐 Proxy ativo: %s", config.PROXY.get("server"))
        if getattr(config, "USAR_CHROME_REAL", False):
            kwargs["channel"] = "chrome"     # usa o Chrome instalado (menos detectável)
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(config.USER_DATA_DIR, **kwargs)
        except Exception as e:
            if "channel" in kwargs:          # Chrome não encontrado → cai pro Chromium
                log.warning("Chrome real não encontrado (%s); usando Chromium do Playwright.", e)
                kwargs.pop("channel")
                self.ctx = self._pw.chromium.launch_persistent_context(config.USER_DATA_DIR, **kwargs)
            else:
                raise
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def fechar(self):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def __enter__(self):
        return self.abrir()

    def __exit__(self, *a):
        self.fechar()

    # ───────────────── navegação / sessão ─────────────────
    def ir(self, url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(1500)

    def _cookies(self):
        """Cookies do instagram.com via Playwright (enxerga HttpOnly, ao contrário do JS)."""
        try:
            cks = self.ctx.cookies("https://www.instagram.com")
        except Exception:
            cks = self.ctx.cookies()
        return {c["name"]: c["value"] for c in cks}

    def logado(self) -> bool:
        # sessionid é HttpOnly → invisível pro document.cookie; lê pelo contexto.
        return bool(self._cookies().get("sessionid"))

    def importar_cookies(self, cookies):
        """Injeta cookies (do navegador normal) no perfil — evita o login/reCAPTCHA."""
        self.ctx.add_cookies(cookies)
        self.ir("https://www.instagram.com/")
        return self.logado()

    def carregar_tokens(self):
        self.tokens = self.page.evaluate(JS_TOKENS)
        ck = self._cookies()
        # csrftoken/ds_user_id não são HttpOnly, mas pegamos do contexto como fonte confiável
        self.tokens["csrf"] = self.tokens.get("csrf") or ck.get("csrftoken")
        self.tokens["dsuser"] = self.tokens.get("dsuser") or ck.get("ds_user_id")
        self.tokens["av"] = self.tokens.get("av") or ck.get("ds_user_id")
        self.tokens["jazoest"] = _jazoest(self.tokens.get("dtsg") or "")
        falta = [k for k in ("csrf", "dtsg", "lsd") if not self.tokens.get(k)]
        if falta:
            log.warning("Tokens ausentes: %s (algumas chamadas podem falhar). "
                        "Confira se a página da thread carregou logada.", falta)
        return self.tokens

    def _base(self):
        return {"appid": config.IG_APP_ID, "asbd": config.ASBD_ID,
                "csrf": self.tokens.get("csrf"), "claim": self.tokens.get("claim", "0")}

    # ───────────────── operações de alto nível ─────────────────
    def ler_mensagens(self, paginas=config.MAX_PAGINAS_MENSAGENS, debug_dump=False,
                      parar_na_reacao=False):
        """Retorna lista de nodes de mensagem (newest->oldest) varrendo N páginas.

        Se `parar_na_reacao=True`, para a paginação assim que encontra o primeiro
        post com QUALQUER reação (o boundary) — não precisa varrer a thread inteira.
        """
        nodes = []
        after = None
        for i in range(paginas):
            variables = {
                "after": after, "before": None, "first": 20, "last": None,
                "newer_than_message_id": None, "older_than_message_id": None,
                "id": config.THREAD_ID,
                "__relay_internal__pv__IGDInitialMessagePageCountrelayprovider": 20,
            }
            res = self.page.evaluate(JS_GRAPHQL, {
                **self._base(), "av": self.tokens.get("av"),
                "dtsg": self.tokens.get("dtsg"), "lsd": self.tokens.get("lsd"),
                "jazoest": self.tokens.get("jazoest"),
                "friendly": "IGDMessageListOffMsysQuery",
                "doc_id": DOC_MESSAGE_LIST,
                "variables": json.dumps(variables, separators=(",", ":")),
            })
            checar_bloqueio(res["status"], res["text"])
            data = _parse_json(res["text"])
            if debug_dump and i == 0:
                import os
                os.makedirs(config.OUTPUT_DIR, exist_ok=True)
                with open(f"{config.OUTPUT_DIR}/debug_messages.json", "w",
                          encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                log.info("debug: dump da 1ª página em output/debug_messages.json")
            try:
                sm = data["data"]["fetch__SlideThread"]["as_ig_direct_thread"]["slide_messages"]
            except (KeyError, TypeError):
                log.error("Resposta de mensagens em formato inesperado. "
                          "Rode com --debug para inspecionar.")
                break
            edges = sm.get("edges", [])
            nodes.extend(e["node"] for e in edges)
            if parar_na_reacao and any(tem_reacao(e["node"]) for e in edges):
                log.info("Boundary (1ª reação) encontrado na página %d; parando paginação.", i)
                break
            pi = sm.get("page_info") or {}
            after = pi.get("end_cursor")
            if not pi.get("has_next_page") or not after:
                break
            self.page.wait_for_timeout(800)
        return nodes

    def get_likers(self, media_id):
        """Lista de usuários que curtiram (uma página; o endpoint /likers/ já devolve o conjunto)."""
        users = []
        url = f"https://www.instagram.com/api/v1/media/{media_id}/likers/"
        res = self.page.evaluate(JS_API_GET, {**self._base(), "url": url})
        checar_bloqueio(res["status"], res["text"])
        if res["status"] != 200:
            log.warning("likers HTTP %s para media %s", res["status"], media_id)
            return users
        data = _parse_json(res["text"])
        users.extend(data.get("users", []))
        return users

    def seguir(self, user_id, tentativas=None):
        """Executa o follow via mutation GraphQL `usePolarisFollowMutation` (o caminho
        REAL do instagram.com web — capturado do clique manual; ver API_REFERENCE.md).

        Retorna um dict no formato `{"friendship_status": {...}, "status": "ok"}`
        (compatível com quem chama em main.py).

        Bloqueio REAL (feedback_required/spam/checkpoint/429) → BloqueioDetectado (para).
        Resposta transitória (5xx/HTML/vazia/sem friendship_status) → tenta de novo
        recarregando os tokens; se esgotar, levanta ErroTransitorio (quem chama pula).
        """
        tentativas = tentativas or getattr(config, "TENTATIVAS_POR_FOLLOW", 3)
        ult = None
        for t in range(tentativas):
            variables = {
                "target_user_id": str(user_id),
                "container_module": "profile",
                "nav_chain": "",
            }
            try:
                res = self.page.evaluate(JS_GRAPHQL, {
                    **self._base(), "av": self.tokens.get("av"),
                    "dtsg": self.tokens.get("dtsg"), "lsd": self.tokens.get("lsd"),
                    "jazoest": self.tokens.get("jazoest"),
                    "friendly": "usePolarisFollowMutation",
                    "doc_id": DOC_FOLLOW,
                    "variables": json.dumps(variables, separators=(",", ":")),
                })
            except Exception as e:
                # 'Failed to fetch' & afins de REDE → transitório: pula esse follow e segue.
                # Browser fechado/crash → propaga (não dá pra continuar a run).
                if not _fetch_transitorio(str(e)):
                    raise
                ult = (None, str(e)[:120])
                if t < tentativas - 1:
                    log.warning("  ~ follow %s: rede falhou no fetch (%s) — tentativa %d/%d",
                                user_id, str(e).split(":")[-1].strip()[:50], t + 1, tentativas)
                    self.page.wait_for_timeout(random.randint(3000, 8000))
                    try:
                        self.carregar_tokens()
                    except Exception:
                        pass
                    continue
                break                                     # esgotou → ErroTransitorio lá embaixo
            checar_bloqueio(res["status"], res["text"])   # bloqueio real → para (sem retry)
            st = res.get("status")
            try:
                data = _parse_json(res["text"])
            except Exception:
                ult = (st, (res.get("text") or "").strip())
                data = None

            if data is not None:
                fr = ((data.get("data") or {}).get("xdt_create_friendship")) or {}
                fs = fr.get("friendship_status")
                if fs:                                   # sucesso → devolve no formato esperado
                    return {"friendship_status": fs, "status": "ok"}
                # GraphQL 200 mas sem friendship_status: pode ser bloqueio mascarado em `errors`
                errs = data.get("errors") or []
                txt_err = json.dumps(errs, ensure_ascii=False).lower()
                if any(k in txt_err for k in ("feedback_required", "checkpoint",
                                              "challenge", "spam", "blocked", "rate_limit")):
                    raise BloqueioDetectado(f"follow recusado pelo IG (GraphQL errors): {str(errs)[:200]}")
                ult = (st, json.dumps(data, ensure_ascii=False)[:200])

            if t < tentativas - 1:
                log.warning("  ~ follow %s: HTTP %s sem friendship_status (transitório) — "
                            "tentativa %d/%d, recarregando tokens e repetindo",
                            user_id, st, t + 1, tentativas)
                self.page.wait_for_timeout(random.randint(3000, 8000))
                try:
                    self.carregar_tokens()               # tokens podem ter rotacionado
                except Exception:
                    pass
        st, corpo = ult if ult else (None, "")
        detalhe = f'corpo: "{corpo[:120]}"' if corpo else "corpo vazio"
        raise ErroTransitorio(f"HTTP {st} ({explicar_status(st)}) após {tentativas} tentativas — {detalhe}")

    def diagnostico(self, motivo="diag"):
        """Salva screenshot + URL + (se houver) corpo da última resposta, pra você VER
        o que o navegador está mostrando (login? checkpoint? feed normal?)."""
        import os
        from datetime import datetime
        os.makedirs(os.path.join(config.OUTPUT_DIR, "logs"), exist_ok=True)
        base = os.path.join(config.OUTPUT_DIR, "logs", f"diag_{datetime.now():%Y%m%d_%H%M%S}")
        info = {"motivo": motivo}
        try:
            self.ir("https://www.instagram.com/")        # vai pro topo pra revelar login/checkpoint
            info["url"] = self.page.url
            info["logado"] = self.logado()
            self.page.screenshot(path=base + ".png", full_page=False)
            log.error("📸 screenshot do estado salvo em %s.png (logado=%s, url=%s)",
                      base, info["logado"], info["url"])
        except Exception as e:
            log.warning("não consegui tirar screenshot: %s", e)
        return base + ".png"

    def reagir_coracao(self, message_id):
        """Reage ❤️ na mensagem do post dentro da thread."""
        variables = {"input": {
            "emoji": config.HEART_EMOJI, "item_id": "",
            "message_id": message_id, "reaction_status": "created",
            "thread_id": config.THREAD_ID,
        }}
        res = self.page.evaluate(JS_GRAPHQL, {
            **self._base(), "av": self.tokens.get("av"),
            "dtsg": self.tokens.get("dtsg"), "lsd": self.tokens.get("lsd"),
            "jazoest": self.tokens.get("jazoest"),
            "friendly": "IGDirectReactionSendMutation",
            "doc_id": DOC_REACTION,
            "variables": json.dumps(variables, separators=(",", ":")),
        })
        checar_bloqueio(res["status"], res["text"])
        return _parse_json(res["text"])


# ───────────── parsing de mensagens (post compartilhado) ─────────────
_URL_RE = re.compile(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]{5,})")
_CODE_RE = re.compile(r'"(?:code|shortcode)":"([A-Za-z0-9_-]{5,})"')
_AUTOR_RE = re.compile(r'"(?:xmaHeaderTitle|header_title_text|owner_username|username)":"([^"]+)"')


def _autor(blob):
    m = _AUTOR_RE.search(blob)
    return m.group(1) if m else "?"


def extrair_post(node):
    """
    De um node de mensagem, tenta achar o post compartilhado.
    Retorna (code, media_id, autor) ou (None, None, None) se não for post
    ou estiver indisponível (privado/excluído).
    """
    if node.get("content_type") != "MESSAGE_INLINE_SHARE":
        return None, None, None
    blob = json.dumps(node.get("content") or {}, ensure_ascii=False, separators=(",", ":"))
    autor = _autor(blob)
    m = _URL_RE.search(blob)
    if m:
        code = m.group(2)
        return code, code_to_pk(code), autor
    m = _CODE_RE.search(blob)
    if m:
        code = m.group(1)
        return code, code_to_pk(code), autor
    return None, None, None   # placeholder "Mensagem indisponível" / privado/excluído


def tem_reacao(node):
    """A mensagem já tem QUALQUER reação de QUALQUER conta?

    Regra do usuário: qualquer reação = post já processado.
    Formato real (da captura ao vivo):
      reactions: [{"reaction": "❤", "sender_fbid": "17842090599502284"}, ...]
    """
    return bool(node.get("reactions"))
