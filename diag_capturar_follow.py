"""
Captura a requisição REAL de follow que o instagram.com dispara ao clicar "Seguir".

Por quê (histórico, já resolvido): descobriu-se aqui que o follow do web moderno vai
por uma mutation GraphQL — hoje implementada em `ig.py` (usePolarisFollowMutation),
não mais pelo `POST /api/v1/friendships/create/` (REST legado, que devolvia shell HTML).
Mantida pra recapturar caso o IG mude o doc_id/payload.

Uso:
  python diag_capturar_follow.py <username>

  <username> = um perfil PÚBLICO que você AINDA NÃO segue (ex: alguém aleatório).
  A ferramenta abre o perfil, clica em "Seguir" e grava a chamada de rede.
  (Depois você pode desseguir manualmente — ela não desfaz sozinha.)

Saída: imprime no console E salva em output/logs/capture_follow.json
"""
import json
import os
import re
import sys

import config
from ig import IG
from safety import log


def _resumir_req(req):
    try:
        post = req.post_data
    except Exception:
        post = None
    return {
        "method": req.method,
        "url": req.url,
        "headers": dict(req.headers),
        "post_data": post,
    }


def capturar(username):
    alvo_re = re.compile(r"(friendships/create|/api/graphql)", re.I)
    with IG() as ig:
        page = ig.page
        ig.ir(f"https://www.instagram.com/{username}/")
        if not ig.logado():
            log.error("Sem sessão logada. Importe os cookies antes (--import-cookies no main.py).")
            return

        # Espera o cabeçalho do perfil renderizar (SPA). Sem isso o botão ainda não existe.
        try:
            page.wait_for_selector("header", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2500)

        # Acha o botão "Seguir" (pt-BR) / "Follow" (en). O IG renderiza como <div role=button>
        # OU <button>, e o texto exato pode ter espaços. Em perfil já seguido aparece
        # "Seguindo"/"Following" — aí não dá pra capturar; escolha outro perfil.
        # Estratégia: pega qualquer elemento clicável cujo texto seja exatamente o rótulo.
        RX = re.compile(r"^\s*(Seguir|Follow|Follow Back|Seguir de volta)\s*$", re.I)
        botao = None
        cand = page.locator("button, [role='button'], div[role='button'], a[role='button']")
        try:
            n = cand.count()
        except Exception:
            n = 0
        rotulos = []
        for i in range(n):
            el = cand.nth(i)
            try:
                txt = (el.inner_text() or "").strip()
            except Exception:
                continue
            if txt:
                rotulos.append(txt)
            if botao is None and RX.match(txt):
                try:
                    if el.is_visible():
                        botao = el
                except Exception:
                    pass

        if botao is None:
            log.error("Não achei o botão 'Seguir' em @%s. Você já segue esse perfil, "
                      "ou o rótulo é outro.", username)
            # Mostra os rótulos clicáveis encontrados — assim sabemos o texto exato.
            unicos = []
            for r in rotulos:
                r1 = r.replace("\n", " | ")[:40]
                if r1 and r1 not in unicos:
                    unicos.append(r1)
            log.error("Botões/clicáveis visíveis na página: %s", unicos[:30] or "(nenhum)")
            ig.diagnostico("sem_botao_seguir")
            return

        log.info("Clicando 'Seguir' em @%s e capturando a requisição de rede…", username)
        capturas = []
        try:
            with page.expect_request(lambda r: r.method == "POST" and bool(alvo_re.search(r.url)),
                                     timeout=15000) as info_req:
                botao.click()
            req = info_req.value
            capturas.append(_resumir_req(req))
            # tenta capturar a resposta correspondente
            try:
                resp = req.response()
                if resp:
                    corpo = resp.text()
                    capturas[-1]["response"] = {
                        "status": resp.status,
                        "url": resp.url,
                        "content_type": resp.headers.get("content-type"),
                        "body_head": corpo[:800],
                    }
            except Exception as e:
                capturas[-1]["response_erro"] = str(e)
        except Exception as e:
            log.error("Não capturei nenhuma chamada de follow em 15s: %s", e)
            log.error("Pode ser que o perfil já estivesse seguido, ou o botão era outro.")
            ig.diagnostico("captura_falhou")
            return

        os.makedirs(os.path.join(config.OUTPUT_DIR, "logs"), exist_ok=True)
        destino = os.path.join(config.OUTPUT_DIR, "logs", "capture_follow.json")
        with open(destino, "w", encoding="utf-8") as f:
            json.dump(capturas, f, ensure_ascii=False, indent=2)

        log.info("✓ Capturado! Salvo em %s", destino)
        log.info("─────────── REQUISIÇÃO REAL DO FOLLOW ───────────")
        for c in capturas:
            log.info("  %s %s", c["method"], c["url"])
            if c.get("post_data"):
                log.info("  body: %s", c["post_data"][:600])
            r = c.get("response") or {}
            log.info("  → resposta: HTTP %s  ct=%s", r.get("status"), r.get("content_type"))
            log.info("  → corpo (início): %s", (r.get("body_head") or "")[:300])
        log.info("─────────────────────────────────────────────────")
        log.info("Me mande esse output (ou o capture_follow.json) que eu acerto o seguir() no ig.py.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python diag_capturar_follow.py <username>  "
              "(um perfil que você NÃO segue)")
        sys.exit(1)
    capturar(sys.argv[1].strip().lstrip("@"))
