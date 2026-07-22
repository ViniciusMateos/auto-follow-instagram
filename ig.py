"""
Cliente do Instagram web.

Estratégia: dirigir um Chrome logado via Playwright e fazer TODAS as chamadas de
API de dentro do contexto da própria página logada (fetch same-origin). Assim os
cookies, fingerprint e headers são os do navegador real — e não precisamos
reimplantar login nem assinar requests.

Endpoints/payloads vêm da captura real (ver API_REFERENCE.md).
"""
import json
import os
import re
import random

from playwright.sync_api import sync_playwright

import config
from safety import log, checar_bloqueio, BloqueioDetectado, ErroTransitorio, explicar_status

# doc_ids das persisted queries (Relay). ⚠️ O IG ROTACIONA esses ids a cada poucas
# semanas. Quando o id fica velho, a query começa a tomar 1357005 ("sua solicitação
# não pôde ser processada" — HTTP 200 + corpo de ~329 bytes) DEPOIS de algumas páginas,
# e a run morre. FOI ISSO (não "burst") que derrubava a paginação da thread: o
# DOC_MESSAGE_LIST tinha ficado defasado. Conferido na captura real de 17/jul/2026
# (insta dm.saz): o navegador paginou 14 páginas seguidas SEM rate limit, mesma conta,
# mesmo ritmo (~2s) — a ÚNICA diferença pro worker era o doc_id.
# Dá pra trocar sem editar código pela env IG_DOC_* ; e o ler_mensagens ainda tenta
# redescobrir o id vivo do bundle da página se este falhar (_descobrir_doc_id).
DOC_MESSAGE_LIST = os.environ.get("IG_DOC_MESSAGE_LIST") or "27502152406082940"  # IGDMessageListOffMsysQuery
DOC_REACTION = os.environ.get("IG_DOC_REACTION") or "24374451552236906"          # IGDirectReactionSendMutation
DOC_FOLLOW = os.environ.get("IG_DOC_FOLLOW") or "26508036048874888"              # usePolarisFollowMutation


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
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 25000);   // proxy morto NÃO pendura pra sempre
  try {
    const r = await fetch(p.url, { credentials: 'include', signal: ctrl.signal, headers: {
      'x-ig-app-id': p.appid, 'x-asbd-id': p.asbd, 'x-csrftoken': p.csrf,
      'x-requested-with': 'XMLHttpRequest', 'x-ig-www-claim': p.claim,
    }});
    return { status: r.status, text: await r.text() };
  } finally { clearTimeout(to); }
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
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 25000);   // proxy morto NÃO pendura pra sempre
  try {
    const r = await fetch('/api/graphql', {
      method: 'POST', credentials: 'include', signal: ctrl.signal, headers: {
        'content-type': 'application/x-www-form-urlencoded',
        'x-fb-friendly-name': p.friendly, 'x-csrftoken': p.csrf,
        'x-asbd-id': p.asbd, 'x-ig-app-id': p.appid,
      }, body: body.toString() });
    return { status: r.status, text: await r.text() };
  } finally { clearTimeout(to); }
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
        "err_network", "err_internet", "err_connection", "err_timed_out", "err_name_not_resolved",
        "abort", "err_proxy"))   # abort = timeout do AbortController; err_proxy = túnel caiu


class IG:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._pw = None
        self.ctx = None
        self.page = None
        self.tokens = {}
        self._doc_msg = DOC_MESSAGE_LIST   # doc_id vivo da paginação (auto-curado se rotacionar)
        self._doc_msg_curado = False       # já tentou redescobrir o id vivo neste run?
        self._www_claim = None             # x-ig-set-www-claim capturado das respostas do IG

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
            log.info("Proxy ativo: %s", config.PROXY.get("server"))
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
        # captura o www-claim REAL que o IG manda no header das respostas. Sem ele (mandando
        # "0"), o /likers/ devolve a versão DEGRADADA (~13 de 1198); com o claim real, o IG
        # devolve a página cheia (~55-98), igual ao navegador. Ver captura "captura de likes".
        self.page.on("response", self._grab_claim)
        self._restaurar_sessao()   # o perfil não guarda cookie; a sessão vem do arquivo
        return self

    def _grab_claim(self, resp):
        try:
            v = resp.headers.get("x-ig-set-www-claim")
            if v and v.startswith("hmac."):
                self._www_claim = v
        except Exception:
            pass

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
    def ir(self, url, timeout=30000):
        # 30s (era 60s fixo): as requisições saem pelo túnel reverso até o PC de casa; um goto
        # que não resolveu em 30s não vai resolver — melhor falhar rápido do que pendurar meio
        # minuto por página.
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        self.page.wait_for_timeout(1500)

    def visitar_post(self, code, timeout=20000):
        """Abre a página do post só pra dar um 'dwell' humano antes de seguir os curtidores.
        NÃO é essencial — os curtidores vêm da API (get_likers), não desta página. Por isso é
        NÃO-FATAL: pelo túnel a página do post às vezes estoura o timeout; se estourar, segue
        direto pros curtidores em vez de DERRUBAR o run (era o 'Page.goto Timeout 60000ms' que
        matava tudo logo depois da paginação varrer o backlog inteiro)."""
        try:
            self.ir(f"https://www.instagram.com/p/{code}/", timeout=timeout)
            return True
        except Exception as e:
            log.warning("  ~ página do post %s não abriu a tempo — sigo pros curtidores (%s)",
                        code, str(e).splitlines()[0][:60])
            return False

    def _cookies(self):
        """Cookies do instagram.com via Playwright (enxerga HttpOnly, ao contrário do JS)."""
        try:
            cks = self.ctx.cookies("https://www.instagram.com")
        except Exception:
            cks = self.ctx.cookies()
        return {c["name"]: c["value"] for c in cks}


    def usuario(self):
        """@username da conta logada AGORA.

        Vem do /data/shared_data/ (o viewer), que é a fonte que o próprio IG usa. O
        ds_user_id do cookie é só um número — e saber QUAL conta está rodando importa:
        já rodamos com a conta errada sem perceber.

        Devolve "" se não conseguir (nunca derruba a run por causa disso).
        """
        try:
            return self.page.evaluate("""async () => {
                const r = await fetch('/data/shared_data/');
                if (!r.ok) return '';
                const j = await r.json();
                return (j.config && j.config.viewer && j.config.viewer.username) || '';
            }""") or ""
        except Exception:
            return ""

    def logado(self) -> bool:
        # sessionid é HttpOnly → invisível pro document.cookie; lê pelo contexto.
        return bool(self._cookies().get("sessionid"))

    def importar_cookies(self, cookies):
        """Injeta os cookies do navegador normal e, se logou, GRAVA a sessão em disco.

        Não dá pra confiar no browser_profile: o Chromium deste server não persiste cookie
        (testado). Por isso a sessão é salva em SESSION_FILE e reinjetada a cada abrir().
        """
        self.ctx.add_cookies(cookies)
        # navega pra "assentar" a sessão, mas NÃO-FATAL: pelo túnel o instagram.com às vezes
        # estoura o timeout, e isso NÃO quer dizer que a sessão é ruim — os cookies já foram
        # injetados e o logado() checa o COOKIE, não a página. Tenta 2x com folga e segue.
        for _tent in range(2):
            try:
                self.ir("https://www.instagram.com/", timeout=45000)
                break
            except Exception as e:
                log.warning("~ instagram.com demorou a abrir (%d/2): %s — sigo pro cookie",
                            _tent + 1, str(e).splitlines()[0][:50])
        if not self.logado():
            return False
        self.salvar_sessao()
        return True

    def salvar_sessao(self):
        """Grava os cookies atuais do instagram.com — é isso que sobrevive entre execuções."""
        try:
            cks = self.ctx.cookies("https://www.instagram.com")
            with open(config.SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(cks, f)
            os.chmod(config.SESSION_FILE, 0o600)   # é credencial: só o dono lê
            log.info("Sessão salva (%d cookies).", len(cks))
        except Exception as e:
            log.warning("Não consegui salvar a sessão: %s", e)

    def _restaurar_sessao(self):
        """Reinjeta a sessão salva no contexto recém-aberto. Silencioso quando não há."""
        if not os.path.exists(config.SESSION_FILE):
            return
        try:
            with open(config.SESSION_FILE, encoding="utf-8") as f:
                cks = json.load(f)
            if cks:
                self.ctx.add_cookies(cks)
        except Exception as e:
            log.warning("Não consegui restaurar a sessão salva: %s", e)

    def carregar_tokens(self):
        """Relê os tokens da página. Só substitui os atuais se a leitura vier COMPLETA —
        uma releitura ruim (página em estado esquisito) não pode derrubar tokens que
        estavam funcionando."""
        anteriores = dict(self.tokens or {})
        self.tokens = self.page.evaluate(JS_TOKENS)
        ck = self._cookies()
        # csrftoken/ds_user_id não são HttpOnly, mas pegamos do contexto como fonte confiável
        self.tokens["csrf"] = self.tokens.get("csrf") or ck.get("csrftoken")
        self.tokens["dsuser"] = self.tokens.get("dsuser") or ck.get("ds_user_id")
        self.tokens["av"] = self.tokens.get("av") or ck.get("ds_user_id")
        self.tokens["jazoest"] = _jazoest(self.tokens.get("dtsg") or "")
        falta = [k for k in ("csrf", "dtsg", "lsd") if not self.tokens.get(k)]
        if falta and anteriores:
            # a releitura veio pior que o que já tínhamos → fica com o antigo
            for k in falta:
                if anteriores.get(k):
                    self.tokens[k] = anteriores[k]
            falta = [k for k in ("csrf", "dtsg", "lsd") if not self.tokens.get(k)]
        if falta:
            log.warning("Tokens ausentes: %s (algumas chamadas podem falhar). "
                        "Confira se a página da thread carregou logada.", falta)
        return self.tokens

    def preparar_thread(self):
        """Navega pra thread e deixa tudo pronto pra ler as mensagens: valida o login,
        regrava a sessão fresca, loga a conta e carrega os tokens. Devolve False se a
        sessão caiu.

        Serve pra paginação DIRETA (ler_mensagens). Diferente do scroll, a paginação não
        depende do 'carregador de mensagens antigas' do IG — é só um POST /api/graphql
        same-origin. Por isso aqui NÃO existe a fragilidade do 'não navegar antes': é um
        load logado normal, e estar na página da thread ainda deixa o bundle do DM
        carregado (é dele que o _descobrir_doc_id lê o id vivo quando precisa)."""
        # A thread do DM é PESADA e pelo túnel o goto às vezes estoura o timeout. NÃO pode ser
        # fatal: os tokens vêm do HTML (que já carregou o suficiente) e a paginação é fetch
        # graphql (não precisa da thread 100% renderizada). Tenta 2x com timeout generoso e,
        # no pior caso, segue com o que carregou — melhor do que derrubar o run na abertura.
        for tent in range(2):
            try:
                self.ir(config.THREAD_URL, timeout=45000)
                break
            except Exception as e:
                log.warning("  ~ thread demorou a abrir pelo túnel (%d/2): %s", tent + 1,
                            str(e).splitlines()[0][:55])
                if tent == 1:
                    log.warning("  ~ sigo com o que carregou (tokens vêm do HTML, paginação é fetch)")
        if not self.logado():
            return False
        self.salvar_sessao()                 # cookie fresco + identifica a conta
        log.info("Conta: @%s", self.usuario() or "?")
        self.carregar_tokens()
        return True

    def _descobrir_doc_id(self, friendly="IGDMessageListOffMsysQuery"):
        """Best-effort: lê o doc_id ATUAL da persisted query direto dos bundles JS que a
        página já baixou. O IG registra cada operação assim (visto na captura):
            __d("IGDMessageListOffMsysQuery_instagramRelayOperation",[],
                (function(t,n,r,o,a,i){a.exports="27502152406082940"}),null);
        Como o id rotaciona, hardcodar faz o bot morrer com 1357005 quando muda — aqui a
        gente pega o valor VIVO. Não-fatal: devolve None se não achar (fica o hardcoded).
        Só é chamado quando a paginação FALHA (id suspeito de velho); não pesa na run boa."""
        js = r"""
        async (friendly) => {
          const re = new RegExp(friendly + '_\\w+RelayOperation.{0,200}?exports=\"(\\d+)\"');
          const urls = performance.getEntriesByType('resource').map(r => r.name)
            .filter(u => u.endsWith('.js') && (u.includes('cdninstagram') || u.includes('/rsrc.php/')));
          let n = 0;
          for (const u of urls) {
            if (n++ > 60) break;
            let t;
            try { t = await (await fetch(u, { credentials: 'omit' })).text(); }
            catch (e) { continue; }
            const m = t.match(re);
            if (m) return m[1];
          }
          return null;
        }
        """
        try:
            return self.page.evaluate(js, friendly)
        except Exception as e:
            log.warning("  ~ não consegui redescobrir o doc_id vivo (%s)", str(e)[:60])
            return None

    def _base(self):
        # claim REAL capturado das respostas > o do sessionStorage > "0". É o que faz o
        # /likers/ devolver a lista cheia em vez da versão capada.
        claim = self._www_claim or self.tokens.get("claim") or "0"
        return {"appid": config.IG_APP_ID, "asbd": config.ASBD_ID,
                "csrf": self.tokens.get("csrf"), "claim": claim}

    # ───────────────── operações de alto nível ─────────────────
    def _gql(self, arg, oque="chamada", tentativas=3):
        """page.evaluate do GraphQL com RETRY em falha de rede.

        O proxy residencial dá blip: um "Failed to fetch" isolado não pode derrubar a run
        inteira (já aconteceu). Repetir é seguro aqui porque estas chamadas são LEITURA
        (idempotentes) — o `seguir()` tem o tratamento dele, que PULA em vez de repetir,
        pra não arriscar seguir duas vezes.

        Browser fechado/crash não é transitório: propaga na hora (a run não continua mesmo).
        """
        ult = None
        for t in range(tentativas):
            try:
                return self.page.evaluate(JS_GRAPHQL, arg)
            except Exception as e:
                if not _fetch_transitorio(str(e)):
                    raise
                ult = e
                if t < tentativas - 1:
                    log.warning("  ~ %s: rede falhou (%s) — tentativa %d/%d",
                                oque, str(e).split(":")[-1].strip()[:50], t + 1, tentativas)
                    self.page.wait_for_timeout(random.randint(2000, 6000))
                    # NÃO recarrega os tokens aqui: carregar_tokens() SOBRESCREVE self.tokens,
                    # e se a releitura vier sem csrf (a página pode estar num estado ruim justo
                    # depois de uma falha de rede) os tokens que funcionavam são perdidos e TODAS
                    # as chamadas seguintes quebram. Um blip de rede não invalida o dtsg.
        raise ult

    def _no_bloco_reagido(self, capturados, minimo):
        """Os posts mais ANTIGOS que já capturamos são todos reagidos?

        Se sim, passamos da fronteira e não precisa subir mais — é o equivalente ao
        "página inteira reagida" da paginação antiga.
        """
        nodes = sorted(capturados.values(), key=_ts)
        posts = [n for n in nodes if extrair_post(n)[0]]
        if len(posts) < minimo:
            return False
        return all(tem_reacao(n) for n in posts[:minimo])

    def _centro_da_lista(self):
        """(x, y) do centro da lista de mensagens, pra pôr o ponteiro em cima dela."""
        r = self.page.evaluate("""() => {
            let alvo = null;
            document.querySelectorAll('div').forEach(d => {
                if (d.scrollHeight > d.clientHeight + 100 && d.clientHeight > 150) {
                    if (!alvo || d.scrollHeight > alvo.scrollHeight) alvo = d;
                }
            });
            if (!alvo) return null;
            const b = alvo.getBoundingClientRect();
            return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) };
        }""")
        return (r["x"], r["y"]) if r else (None, None)

    def _scroll_top(self):
        """scrollTop da lista (negativo: a lista é column-reverse)."""
        return self.page.evaluate("""() => {
            let alvo = null;
            document.querySelectorAll('div').forEach(d => {
                if (d.scrollHeight > d.clientHeight + 100 && d.clientHeight > 150) {
                    if (!alvo || d.scrollHeight > alvo.scrollHeight) alvo = d;
                }
            });
            return alvo ? alvo.scrollTop : 0;
        }""")

    def _tem_lista(self):
        """A lista de mensagens existe? (inbox fica na casa dos 900px; a thread, milhares)"""
        sh = self.page.evaluate("""() => {
            let maior = 0;
            document.querySelectorAll('div').forEach(d => {
                if (d.scrollHeight > d.clientHeight + 100 && d.clientHeight > 150) {
                    if (d.scrollHeight > maior) maior = d.scrollHeight;
                }
            });
            return maior;
        }""")
        if sh and sh > 2500:
            log.info("Lista de mensagens montada (%dpx).", sh)
            return True
        return False

    def ler_mensagens_scroll(self, max_scrolls=None, estavel_max=None):
        """Lê a thread SUBINDO como humano e colhendo as respostas graphql que a PRÓPRIA
        página dispara ao carregar mensagens antigas.

        Por que assim: pedir as páginas na mão (ler_mensagens) é um padrão de "burst" que
        o IG estrangula — erro 1357005 depois de ~5 páginas, e aí a run inteira morre. O
        scroll natural ele serve sem reclamar. A gente não PEDE nada: só escuta o que o
        navegador baixou sozinho. É o mesmo truque que o brecho-tracker usa pro feed.

        Devolve os nodes novo→antigo (mesma ordem do ler_mensagens).
        """
        max_scrolls = max_scrolls or config.SCROLL_MAX
        estavel_max = estavel_max or config.SCROLL_ESTAVEL_MAX
        capturados = {}
        rejeitados = {}
        alvo_thread = str(config.THREAD_ID)

        def _colher(o):
            if isinstance(o, dict):
                # SÓ mensagens desta thread: o inbox e outras conversas também disparam
                # graphql enquanto a página vive, e node de outra thread vira post fantasma
                # (vi um "code" de 39 chars virar pedido de likers num id inexistente).
                if o.get("message_id") and "timestamp_ms" in o:
                    tf = str(o.get("thread_fbid") or "")
                    if tf == alvo_thread:
                        capturados.setdefault(o["message_id"], o)
                    else:
                        rejeitados.setdefault(o["message_id"], tf or "(sem thread_fbid)")
                for v in o.values():
                    _colher(v)
            elif isinstance(o, list):
                for v in o:
                    _colher(v)

        def _on_response(resp):
            u = resp.url
            if "instagram.com" not in u or "graphql" not in u:
                return
            try:
                _colher(resp.json())
            except Exception:
                pass     # resposta não-JSON: não é pra nós

        self.page.on("response", _on_response)
        try:
            # Uma navegação SÓ, feita AQUI com o listener já pendurado — é isso que faz
            # os dois problemas irem embora de vez:
            #   • se o main navegasse e o método também, o SPA do IG ficava meio-morto e o
            #     carregador de mensagens antigas não armava (sh cravado em 10833);
            #   • se o método NÃO navegasse (o main já tinha ido), o listener era pendurado
            #     tarde e perdia a leva inicial de mensagens (msgs=0).
            # Navegando aqui, com o on("response") já ativo, a 1ª leva é capturada E o
            # carregador arma. O main agora só valida o login (vai pra home), sem tocar na thread.
            self.ir(config.THREAD_URL)          # a PRIMEIRA e ÚNICA navegação (load fresco)
            if not self.logado():
                log.error("Sem sessão logada. Reimporte os cookies.")
                return []
            self.salvar_sessao()                # cookie fresco + identifica a conta
            log.info("Conta: @%s", self.usuario() or "?")
            self.carregar_tokens()
            # Espera FIXA, não "até aparecer algo rolável".
            # O _esperar_lista voltava assim que via >2500px — às vezes 1s — e o scroll
            # começava com a thread meio montada; aí o IG nunca carregava o passado
            # (medido: sh cravado em 10833 a run inteira). Com 12s corridos, cresce.
            # Não troque por polling sem medir de novo.
            self.page.wait_for_timeout(config.THREAD_MONTAGEM_MS)
            if not self._tem_lista():
                log.warning("A lista de mensagens não montou — thread carregou?")
                return []
            estavel = ult = ult_estavel = ult_sh = 0
            for i in range(max_scrolls):
                # Rola o CONTÊINER da lista, não a janela: o DM do IG tem um div rolável
                # interno e a janela não tem scroll nenhum (mouse.wheel não movia nada).
                # Subir carrega as mensagens antigas.
                # scrollTop por JS: a roda do mouse trava em elementos internos da
                # mensagem (testado: parou em -3109 sem chegar ao topo em -10167).
                # ⚠️ Este JS é IDÊNTICO ao do teste que comprovadamente faz o IG carregar
                # (scrollHeight 10833 → 20619). Não "melhore": disparar um Event('scroll')
                # sintético aqui fazia a lista parar de crescer, e passo fixo de 2500 é o
                # que foi medido funcionando.
                moveu = self.page.evaluate("""() => {
                    let a = null;
                    document.querySelectorAll('div').forEach(d => {
                        if (d.scrollHeight > d.clientHeight + 100 && d.clientHeight > 150) {
                            if (!a || d.scrollHeight > a.scrollHeight) a = d;
                        }
                    });
                    if (!a) return null;
                    const antes = a.scrollTop;
                    a.scrollTop = a.scrollTop - 2500;
                    return { antes, depois: a.scrollTop, sh: a.scrollHeight,
                             moveu: a.scrollTop !== antes };
                }""")
                if moveu is None:
                    log.warning("Não achei a lista de mensagens pra rolar — a thread montou?")
                    break
                self.page.wait_for_timeout(random.randint(*config.SCROLL_PAUSA_MS))
                n = len(capturados)
                log.info("  [scroll %d] top %s→%s sh=%s msgs=%d estavel=%d",
                         i + 1, moveu.get("antes"), moveu.get("depois"),
                         moveu.get("sh"), n, estavel)
                if rejeitados and i == 6:
                    from collections import Counter
                    log.info("  thread_fbid dos rejeitados: %s | eu procuro: %r",
                             dict(Counter(rejeitados.values()).most_common(3)), alvo_thread)
                if n > ult:
                    log.info("  … subindo: +%d msgs (total %d)", n - ult, n)
                    ult = n

                # ⚠️ O "estável" mede o CONTÊINER, não as mensagens.
                # O IG só busca o passado quando você ENCOSTA no topo do que já está
                # carregado — os scrolls do meio passeiam por conteúdo que já veio e não
                # trazem nada. Contando "sem mensagem nova" como estável, o bot desistia
                # a uma volta de chegar lá (foi o que aconteceu: parava sempre em 20).
                sh = moveu.get("sh") or 0
                # PROGRESSO = qualquer uma destas mudou desde a última volta: a posição, a
                # contagem de mensagens, OU a altura da lista (o IG trouxe passado).
                # O segredo medido: depois de bater no limite (top parado), o sh só cresce
                # 2-4 voltas DEPOIS — então NÃO se pode contar "estável" só porque o top
                # travou. Enquanto o sh puder crescer, continua empurrando.
                houve_progresso = (moveu.get("moveu") or n > ult_estavel or sh > ult_sh)
                ult_estavel = n
                ult_sh = max(ult_sh, sh)
                if houve_progresso:
                    estavel = 0
                else:
                    estavel += 1     # top E sh parados: aí sim pode ser o fim de verdade
                # ── O CRITÉRIO É A REAÇÃO ──
                # Sobe até encontrar o bloco contíguo de posts já reagidos. É o mesmo
                # critério de sempre: o ❤ no chat é a memória do bot.
                if self._no_bloco_reagido(capturados, config.SCROLL_BLOCO_MIN):
                    log.info("Cheguei no bloco já reagido — %d mensagens varridas.", n)
                    break
                # "estável" NÃO é critério: é rede de segurança pro fim REAL da thread.
                # Exige as duas coisas — o contêiner travado E nenhuma mensagem nova —
                # senão ele dispara enquanto o IG ainda está buscando e a gente desiste
                # justo antes da rajada chegar.
                if estavel >= estavel_max:
                    log.warning("Subi %d mensagens e o topo da thread não trouxe mais nada, "
                                "sem achar post reagido. Vou tratar como início da thread.", n)
                    break
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:
                pass
        return sorted(capturados.values(), key=_ts, reverse=True)

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
            # Repete a MESMA página se o corpo vier fora do formato. Acontece logo depois
            # de um blip de rede: o fetch volta 200 mas com resposta estranha. Desistir aqui
            # trunca a varredura no meio — e varredura truncada = fronteira errada = posts
            # pulados pra sempre. Ler é idempotente, então repetir é seguro.
            data = None
            for tent in range(5):
                res = self._gql({
                    **self._base(), "av": self.tokens.get("av"),
                    "dtsg": self.tokens.get("dtsg"), "lsd": self.tokens.get("lsd"),
                    "jazoest": self.tokens.get("jazoest"),
                    "friendly": "IGDMessageListOffMsysQuery",
                    "doc_id": self._doc_msg,
                    "variables": json.dumps(variables, separators=(",", ":")),
                }, oque=f"ler mensagens (pág {i + 1})")
                checar_bloqueio(res["status"], res["text"])
                d = _parse_json(res["text"])
                if _tem_mensagens(d):
                    data = d
                    break
                # Resposta sem mensagens = rate limit OU doc_id velho. As duas coisas têm a
                # MESMA causa histórica: o id da persisted query rotacionou. Antes de esperar,
                # tenta pegar o id vivo do bundle da página (1x por run); se mudou, repete JÁ
                # com o novo, sem penalidade. É o que impede o bot de morrer de novo quando o
                # IG trocar o doc_id daqui a umas semanas.
                if not self._doc_msg_curado:
                    self._doc_msg_curado = True
                    vivo = self._descobrir_doc_id("IGDMessageListOffMsysQuery")
                    if vivo and vivo != self._doc_msg:
                        log.warning("  ~ doc_id da paginação estava velho (%s) → id vivo do "
                                    "bundle é %s. Atualizei e vou repetir a página.",
                                    self._doc_msg, vivo)
                        self._doc_msg = vivo
                        continue
                if _rate_limited(d):
                    # "Sua solicitação não pôde ser processada" = o IG pedindo calma.
                    # 3s não adianta: aqui tem que esperar de verdade, senão só irrita mais.
                    espera = random.randint(20000, 45000)
                    log.warning("  ~ pág %d: o IG pediu calma (rate limit) — esperando %ds "
                                "(tentativa %d/5)", i + 1, espera // 1000, tent + 1)
                    if tent < 4:
                        self.page.wait_for_timeout(espera)
                    continue
                log.warning("  ~ pág %d: resposta fora do formato (%d bytes) — tentativa %d/5",
                            i + 1, len(res.get("text") or ""), tent + 1)
                if tent < 4:
                    self.page.wait_for_timeout(random.randint(3000, 10000))
            if data is None:
                # NÃO dá pra seguir com varredura truncada: a fronteira sai do bloco contíguo
                # de reagidos, e sem alcançar esse bloco o bot começaria do post errado e
                # PULARIA todos os que ficaram atrás. Melhor a run falhar e você ver.
                raise RuntimeError(
                    f"a página {i + 1} das mensagens não veio no formato esperado em 5 "
                    f"tentativas (varri {len(nodes)} msgs). Sem a thread inteira eu não sei "
                    f"onde é a fronteira — não vou arriscar pular post. Tente de novo.")
            if debug_dump and i == 0:
                import os
                os.makedirs(config.OUTPUT_DIR, exist_ok=True)
                with open(f"{config.OUTPUT_DIR}/debug_messages.json", "w",
                          encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                log.info("debug: dump da 1ª página em output/debug_messages.json")
            # shape já garantido pelo _tem_mensagens acima
            sm = data["data"]["fetch__SlideThread"]["as_ig_direct_thread"]["slide_messages"]
            edges = sm.get("edges", [])
            nodes.extend(e["node"] for e in edges)
            # log por página: você VÊ a thread subindo em vez de ficar no escuro até o fim.
            posts_ate_aqui = sum(1 for n in nodes if extrair_post(n)[0])
            log.info("  pág %d: +%d msgs (total %d, %d posts) — subindo a thread…",
                     i + 1, len(edges), len(nodes), posts_ate_aqui)
            # Para quando a página INTEIRA já está reagida = chegamos no bloco que já foi
            # processado (o bot marca em ordem, então os feitos ficam todos juntos no fundo).
            #
            # Parar na PRIMEIRA reação não serve: basta alguém do grupo reagir fora de ordem
            # num post recente pra paginação parar ali e todos os posts atrás dele nunca mais
            # serem vistos. Aconteceu de verdade: uma reação solta escondeu 165 posts.
            posts_pag = [e["node"] for e in edges if extrair_post(e["node"])[0]]
            if parar_na_reacao and posts_pag and all(tem_reacao(n) for n in posts_pag):
                log.info("Página %d toda reagida — cheguei no bloco já processado; parando.", i + 1)
                break
            pi = sm.get("page_info") or {}
            after = pi.get("end_cursor")
            if not pi.get("has_next_page") or not after:
                break
            # ritmo entre páginas: 800ms fixo é rápido e regular demais — é justo o que
            # dispara o rate limit ao varrer fundo (a thread tem 200+ posts de backlog).
            self.page.wait_for_timeout(random.randint(1500, 3500))
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
        # DIAGNÓSTICO T3: o /likers/ devolve TODOS ou capa? user_count = total real do post.
        # Se total >> devolvidos, o endpoint tá capando e precisa paginar (ou usar o graphql
        # de likes, que rola). next_max_id != None também indica que tem mais página.
        total = data.get("user_count")
        prox = data.get("next_max_id") or data.get("next_max_id_v2")
        log.info("  [likers] devolvidos=%d | user_count=%s | next=%s | chaves=%s",
                 len(users), total, bool(prox), list(data.keys()))
        return users

    def resolver_thread(self, alvo):
        """Acha o thread_id de um chat pelo NOME do grupo (ou @usuário) varrendo o inbox.
        Devolve o thread_id (str) ou None se não achar. Usado quando o chat foi salvo só
        pelo nome (sem thread_id)."""
        alvo_l = str(alvo).strip().lstrip("@").lower()
        if not alvo_l:
            return None
        url = ("https://www.instagram.com/api/v1/direct_v2/inbox/"
               "?visual_message_return_type=unseen&thread_message_limit=1"
               "&persistentBadging=true&limit=50")
        res = self.page.evaluate(JS_API_GET, {**self._base(), "url": url})
        checar_bloqueio(res["status"], res["text"])
        if res["status"] != 200:
            log.warning("inbox HTTP %s ao resolver '%s'", res["status"], alvo)
            return None
        threads = (_parse_json(res["text"]).get("inbox") or {}).get("threads") or []

        def titulo(t):
            return (t.get("thread_title") or "").strip().lower()

        for t in threads:                              # 1) título exato
            if titulo(t) == alvo_l:
                return str(t.get("thread_id"))
        for t in threads:                              # 2) título contém
            if alvo_l in titulo(t):
                return str(t.get("thread_id"))
        for t in threads:                              # 3) @usuário de uma DM 1:1
            for u in (t.get("users") or []):
                if (u.get("username") or "").lower() == alvo_l:
                    return str(t.get("thread_id"))
        return None

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
        res = self._gql({
            **self._base(), "av": self.tokens.get("av"),
            "dtsg": self.tokens.get("dtsg"), "lsd": self.tokens.get("lsd"),
            "jazoest": self.tokens.get("jazoest"),
            "friendly": "IGDirectReactionSendMutation",
            "doc_id": DOC_REACTION,
            "variables": json.dumps(variables, separators=(",", ":")),
        }, oque="reagir")
        checar_bloqueio(res["status"], res["text"])
        return _parse_json(res["text"])


# ───────────── parsing de mensagens (post compartilhado) ─────────────
# Shortcode do IG tem 11 chars (12 em alguns casos). O {5,} sem teto engolia tokens
# gigantes que aparecem no blob e devolvia lixo como "code" — daí o code_to_pk gerava um
# pk inexistente e o get_likers voltava VAZIO (o "Expecting value" que derrubava a run).
# O lookahead faz o token longo NÃO casar (em vez de casar truncado, que seria pior).
_URL_RE = re.compile(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]{5,14})(?![A-Za-z0-9_-])")
_CODE_RE = re.compile(r'"(?:code|shortcode)":"([A-Za-z0-9_-]{5,14})"')
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


# O IG responde isto quando você pagina rápido/fundo demais. Vem com HTTP 200 e corpo
# curtinho — se você não olhar o código, parece "resposta estranha" e some no ruído.
_RATE_LIMIT = 1357005


def _rate_limited(data):
    """A resposta é o rate limit do IG (erro 1357005), e não um erro de formato?"""
    try:
        return int(data.get("error") or 0) == _RATE_LIMIT
    except Exception:
        return False


def _ts(node):
    """timestamp_ms do node como int. O IG manda ora número, ora string — ordenar
    misturando str e int levanta TypeError e derruba a run."""
    try:
        return int(node.get("timestamp_ms") or 0)
    except (TypeError, ValueError):
        return 0


def _tem_mensagens(data):
    """A resposta do GraphQL tem o shape esperado (com a lista de mensagens)?"""
    try:
        data["data"]["fetch__SlideThread"]["as_ig_direct_thread"]["slide_messages"]
        return True
    except (KeyError, TypeError):
        return False


def tem_reacao(node):
    """A mensagem já tem QUALQUER reação de QUALQUER conta?

    Regra do usuário: qualquer reação = post já processado.
    Formato real (da captura ao vivo):
      reactions: [{"reaction": "❤", "sender_fbid": "17842090599502284"}, ...]
    """
    return bool(node.get("reactions"))
