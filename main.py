"""
auto-like-instagram — orquestrador.

Fluxo (igual ao manual):
  1. abre a DM "vai toma no quase nada"
  2. acha o último post JÁ marcado com ❤️ (ou estado local) = ponto de retomada
  3. pega o próximo post sem ❤️, abre, lê as curtidas
  4. segue cada curtidor (pulando já-seguidos/pendentes), respeitando os caps
  5. reage ❤️ no post (marca como feito) e vai pro próximo

Uso:
  python main.py --login            # 1ª vez: faz login manual na janela
  python main.py --dry-run          # simula tudo (lê de verdade, não age) ← FAÇA ISSO 1º
  python main.py                    # roda pra valer
  python main.py --start-after CODE # força começar depois de um post específico
  python main.py --debug            # despeja a 1ª página de mensagens p/ calibração
"""
import argparse
import os
import sys
import traceback
from datetime import datetime

import config
from safety import State, Guard, log, BloqueioDetectado, LimiteAtingido
from ig import IG, extrair_post, tem_reacao

LOGS_ERRO_DIR = os.path.join(config.OUTPUT_DIR, "logs")


def _carregar_cookies(path):
    """Lê um JSON de cookies (ex: extensão Cookie-Editor) e converte pro formato Playwright."""
    import json
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]
    ss_map = {"no_restriction": "None", "unspecified": "Lax", "lax": "Lax",
              "strict": "Strict", "none": "None"}
    out = []
    for c in raw:
        dom = c.get("domain") or ".instagram.com"
        ss = ss_map.get(str(c.get("sameSite", "")).lower(), "Lax")
        ck = {"name": c["name"], "value": c["value"], "domain": dom,
              "path": c.get("path", "/"), "httpOnly": bool(c.get("httpOnly")),
              "secure": bool(c.get("secure", True)), "sameSite": ss}
        exp = c.get("expirationDate") or c.get("expires")
        if exp and not c.get("session"):
            ck["expires"] = int(float(exp))
        out.append(ck)
    return out


def modo_importar_cookies(path):
    cookies = _carregar_cookies(path)
    log.info("Importando %d cookies de %s…", len(cookies), path)
    with IG() as ig:
        if ig.importar_cookies(cookies):
            log.info("✓ Sessão logada! Pode rodar `python main.py --dry-run`.")
        else:
            log.warning("Importou, mas não achei sessionid. Confira se exportou os cookies "
                        "do instagram.com COM a conta logada (precisa do sessionid).")


def imprimir_saldo(guard, motivo=""):
    """Resumo final SEMPRE impresso (fim normal, erro, bloqueio ou Ctrl+C)."""
    extra = f" — {motivo}" if motivo else ""
    log.info("──────────────── SALDO DA EXECUÇÃO%s ────────────────", extra)
    log.info("   seguidas (públicas) ....... %d", guard.seguidos)
    log.info("   solicitadas (privadas) .... %d", guard.pendentes)
    log.info("   puladas (já seguia/etc) ... %d", guard.pulados)
    log.info("   total de ações de follow .. %d", guard.seguidos + guard.pendentes)
    log.info("─────────────────────────────────────────────────────")


def tratar_erro(exc, titulo):
    """Salva o traceback completo num arquivo e mostra só o resumido no console."""
    os.makedirs(LOGS_ERRO_DIR, exist_ok=True)
    nome = "erro_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
    caminho = os.path.join(LOGS_ERRO_DIR, nome)
    try:
        with open(caminho, "w", encoding="utf-8") as f:
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        caminho = "(não consegui salvar o arquivo de erro)"
    log.error("⛔ %s: %s", titulo, str(exc)[:160])
    log.error("   detalhes completos em: %s", caminho)


def modo_login():
    log.info("Abrindo navegador para login manual…")
    with IG() as ig:
        ig.ir("https://www.instagram.com/")
        print("\n>>> Faça login na janela do Chrome. A sessão fica salva em "
              f"'{config.USER_DATA_DIR}'.")
        input(">>> Quando estiver logado e vendo o feed, aperte ENTER aqui… ")
        if ig.logado():
            log.info("Sessão detectada e salva. Pode rodar --dry-run.")
        else:
            log.warning("Não detectei sessionid. Confira se o login concluiu.")


def montar_lista_posts(nodes, state):
    """
    nodes vêm newest->oldest. Devolve lista CRONOLÓGICA (antigo->novo) de TODOS os
    posts compartilhados (de qualquer remetente), com metadados de marcação.
    Um post está "feito" se tem QUALQUER reação OU se já está no estado local.
    """
    posts = []
    for node in reversed(nodes):                 # antigo -> novo
        code, media_id, autor = extrair_post(node)
        if not code:
            continue                              # não é post / indisponível
        mid = node.get("message_id")
        reagido = tem_reacao(node)
        processed = reagido or state.post_processado(code, mid)
        posts.append({"code": code, "media_id": media_id, "message_id": mid,
                      "autor": autor, "hearted": reagido, "processed": processed})
    return posts


def escolher_candidatos(posts, start_after=None):
    """Aplica a regra: a partir do último processado, pega os próximos não feitos."""
    if start_after:
        idxs = [i for i, p in enumerate(posts) if p["code"] == start_after]
        if not idxs:
            log.error("--start-after %s: post não encontrado na varredura.", start_after)
            return []
        return [p for p in posts[idxs[0] + 1:] if not p["processed"]]

    ult_proc = max((i for i, p in enumerate(posts) if p["processed"]), default=None)
    if ult_proc is None:
        if config.START_FROM_OLDEST_SE_VAZIO:
            log.info("Nenhum post marcado ainda; começando do mais antigo.")
            return [p for p in posts if not p["processed"]]
        log.warning("Nenhum post com ❤️ encontrado e START_FROM_OLDEST_SE_VAZIO=False. "
                    "Para iniciar, rode com --start-after <CODE> do último que você já fez "
                    "manualmente (ou ligue a flag no config).")
        return []
    return [p for p in posts[ult_proc + 1:] if not p["processed"]]


def deve_pular_liker(u, state):
    fs = u.get("friendship_status") or {}
    uid = u.get("pk") or u.get("pk_id") or u.get("id")
    if state.ja_seguiu(uid):
        return "já seguido (estado local)"
    if config.PULAR_JA_SEGUIDOS and fs.get("following"):
        return "já seguido"
    if config.PULAR_PENDENTES and fs.get("outgoing_request"):
        return "pedido pendente"
    if not config.SEGUIR_PRIVADOS and (fs.get("is_private") or u.get("is_private")):
        return "privado (config)"
    return None


def processar_post(ig, p, state, guard, dry):
    ig.ir(f"https://www.instagram.com/p/{p['code']}/")          # abre o post (humano)
    guard.dormir(config.DELAY_ACAO_UI, "abrindo post")

    likers = ig.get_likers(p["media_id"])
    log.info("┌─ POST de @%s  (%s) — %d curtidores", p.get("autor") or "?",
             p["code"], len(likers))

    seguidos = pendentes = pulados = 0
    for u in likers:                              # segue TODOS os curtidores
        uid = u.get("pk") or u.get("pk_id") or u.get("id")
        uname = u.get("username", "?")
        motivo = deve_pular_liker(u, state)
        if motivo:
            log.info("│    . pulou @%-28s (%s)", uname, motivo)
            pulados += 1; guard.pulados += 1
            continue
        guard.pode_seguir()                       # levanta LimiteAtingido se estourar
        if dry:
            priv = u.get("is_private") or (u.get("friendship_status") or {}).get("is_private")
            log.info("│    + [dry] %s @%s", "pedido (priv)" if priv else "seguiria", uname)
            if priv: pendentes += 1; guard.pendentes += 1
            else:    seguidos += 1; guard.seguidos += 1
            guard.pos_follow_dry()                # contabiliza p/ o cap ser fiel
            continue
        resp = ig.seguir(uid)
        fs = resp.get("friendship_status") or {}
        if fs.get("following"):                   # pública → virou "Seguindo"
            state.marcar_seguido(uid); seguidos += 1; guard.seguidos += 1
            log.info("│    + seguiu @%s", uname)
            guard.pos_follow()
        elif fs.get("outgoing_request"):          # privada → pedido pendente
            state.marcar_seguido(uid); pendentes += 1; guard.pendentes += 1
            log.info("│    ~ pedido enviado @%s (privado)", uname)
            guard.pos_follow()
        else:
            log.warning("│    ! follow @%s sem confirmação: %s", uname, str(resp)[:140])

    # marca o post como feito: reação no chat + estado local
    if dry:
        log.info("└─ [dry] reagiria — agiria em %d, pulou %d (de @%s)",
                 seguidos + pendentes, pulados, p.get("autor") or "?")
    else:
        ig.ir(config.THREAD_URL)
        guard.dormir(config.DELAY_ACAO_UI, "voltando à thread")
        ig.reagir_coracao(p["message_id"])
        state.marcar_post(p["code"], p["message_id"])
        log.info("└─ post de @%s marcado [reagido] — seguiu %d, pedidos %d (priv), pulou %d",
                 p.get("autor") or "?", seguidos, pendentes, pulados)
    return seguidos + pendentes


def run(dry=False, start_after=None, debug=False, ignorar_janela=False):
    state = State()
    guard = Guard(state, dry_run=dry)

    try:
        guard.checar_janela(ignorar=ignorar_janela)   # sem cooldown por bloqueio
    except LimiteAtingido as e:
        log.info("Não vou rodar agora: %s", e)
        return

    log.info("Abrindo Instagram (%s)…", "DRY-RUN" if dry else "AÇÃO REAL")
    with IG(dry_run=dry) as ig:
        ig.ir(config.THREAD_URL)
        if not ig.logado():
            log.error("Sem sessão logada. Rode `python main.py --login` primeiro.")
            return
        ig.carregar_tokens()

        try:
            nodes = ig.ler_mensagens(debug_dump=debug, parar_na_reacao=True)
            log.info("%d mensagens varridas.", len(nodes))
            posts = montar_lista_posts(nodes, state)
            feitos = [p["code"] for p in posts if p["processed"]]
            log.info("%d posts na thread | %d já marcados.", len(posts), len(feitos))

            candidatos = escolher_candidatos(posts, start_after=start_after)
            limite = config.MAX_POSTS_POR_RUN if config.APLICAR_CAPS else len(candidatos)
            candidatos = candidatos[:limite]
            if not candidatos:
                log.info("Nenhum post novo para processar.")
                return
            if not config.APLICAR_CAPS:
                log.warning("MODO DESCOBERTA: sem cap de follow. Rodando até o IG bloquear. "
                            "(%d posts no backlog)", len(candidatos))
            log.info("Próximos a processar: %s",
                     ", ".join(p["code"] for p in candidatos[:8]) + (" …" if len(candidatos) > 8 else ""))

            for i, p in enumerate(candidatos):
                processar_post(ig, p, state, guard, dry)
                if i < len(candidatos) - 1:
                    guard.dormir(config.DELAY_POST, "entre posts")
            log.info("Backlog deste run concluído.")
        except LimiteAtingido as e:
            log.info("Parando (cap atingido): %s", e)
        except BloqueioDetectado as e:
            tratar_erro(e, "BLOQUEIO do Instagram — parando o run")
        except KeyboardInterrupt:
            log.info("Interrompido manualmente (Ctrl+C).")
        except Exception as e:                        # qualquer outro erro: arquivo + resumo
            tratar_erro(e, "erro inesperado — parando o run")
        finally:
            imprimir_saldo(guard, "simulado" if dry else "")


def main():
    ap = argparse.ArgumentParser(description="auto-like-instagram")
    ap.add_argument("--login", action="store_true", help="login manual (1ª vez)")
    ap.add_argument("--import-cookies", metavar="FILE", help="importa cookies (JSON do Cookie-Editor) e pula o login")
    ap.add_argument("--dry-run", action="store_true", help="simula sem agir")
    ap.add_argument("--debug", action="store_true", help="dump da 1ª página de mensagens")
    ap.add_argument("--start-after", metavar="CODE", help="começar após este shortcode")
    ap.add_argument("--ignore-window", action="store_true", help="ignora janela de horário")
    a = ap.parse_args()

    if a.import_cookies:
        modo_importar_cookies(a.import_cookies)
        return
    if a.login:
        modo_login()
        return
    try:
        run(dry=a.dry_run, start_after=a.start_after, debug=a.debug,
            ignorar_janela=a.ignore_window)
    except KeyboardInterrupt:
        log.info("Interrompido.")
    except Exception as e:                            # rede de segurança final
        tratar_erro(e, "erro fatal")
        sys.exit(2)


if __name__ == "__main__":
    main()
