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
import sys

import config
from safety import State, Guard, log, BloqueioDetectado, LimiteAtingido
from ig import IG, extrair_post, tem_reacao


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
            log.info("│    · pulou @%-28s (%s)", uname, motivo)
            pulados += 1
            continue
        guard.pode_seguir()                       # levanta LimiteAtingido se estourar
        if dry:
            tag = "pedido (priv)" if (u.get("is_private") or (u.get("friendship_status") or {}).get("is_private")) else "seguiria"
            log.info("│    ✓ [dry] %s @%s", tag, uname)
            seguidos += 1
            guard.pos_follow_dry()                # contabiliza p/ o cap ser fiel
            continue
        resp = ig.seguir(uid)
        fs = resp.get("friendship_status") or {}
        if fs.get("following"):                   # pública → virou "Seguindo"
            state.marcar_seguido(uid); seguidos += 1
            log.info("│    ✓ seguiu @%s", uname)
            guard.pos_follow()
        elif fs.get("outgoing_request"):          # privada → pedido pendente
            state.marcar_seguido(uid); pendentes += 1
            log.info("│    ⏳ pedido enviado @%s (privado)", uname)
            guard.pos_follow()
        else:
            log.warning("│    ! follow @%s sem confirmação: %s", uname, str(resp)[:140])

    # marca o post como feito: reação ❤️ + estado local
    if dry:
        log.info("└─ [dry] reagiria ❤️ — agiria em %d, pulou %d (de @%s)",
                 seguidos, pulados, p.get("autor") or "?")
    else:
        ig.ir(config.THREAD_URL)
        guard.dormir(config.DELAY_ACAO_UI, "voltando à thread")
        ig.reagir_coracao(p["message_id"])
        state.marcar_post(p["code"], p["message_id"])
        log.info("└─ ❤️ post de @%s marcado — seguiu %d, pedidos %d (priv), pulou %d",
                 p.get("autor") or "?", seguidos, pendentes, pulados)
    return seguidos + pendentes


def run(dry=False, start_after=None, debug=False, ignorar_janela=False):
    state = State()
    guard = Guard(state, dry_run=dry)

    try:
        guard.checar_cooldown()
        guard.checar_janela(ignorar=ignorar_janela)
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

        nodes = ig.ler_mensagens(debug_dump=debug, parar_na_reacao=True)
        log.info("%d mensagens varridas.", len(nodes))
        posts = montar_lista_posts(nodes, state)
        feitos = [p["code"] for p in posts if p["processed"]]
        log.info("%d posts na thread | %d já marcados.", len(posts), len(feitos))

        candidatos = escolher_candidatos(posts, start_after=start_after)
        limite = config.MAX_POSTS_POR_RUN if config.APLICAR_CAPS else len(candidatos)
        candidatos = candidatos[:limite]
        if not candidatos:
            log.info("Nenhum post novo para processar. 👋")
            return
        if not config.APLICAR_CAPS:
            log.warning("MODO DESCOBERTA: sem cap de follow. Rodando até o IG bloquear. "
                        "(%d posts no backlog)", len(candidatos))
        log.info("Próximos a processar: %s",
                 ", ".join(p["code"] for p in candidatos[:8]) + (" …" if len(candidatos) > 8 else ""))

        try:
            for i, p in enumerate(candidatos):
                processar_post(ig, p, state, guard, dry)
                if i < len(candidatos) - 1:
                    guard.dormir(config.DELAY_POST, "entre posts")
        except LimiteAtingido as e:
            log.info("Parando com elegância: %s", e)
        except BloqueioDetectado as e:
            log.error("⛔ BLOQUEIO detectado pelo Instagram: %s", e)
            log.error("⛔ TRAVOU APÓS %d follows nesta execução (%d nas últimas 24h).",
                      guard.total_follows(), state.follows_ultimo_dia())
            state.ativar_cooldown(config.COOLDOWN_BLOQUEIO_HORAS)
            log.error("Cooldown de %dh ativado. NÃO insista — re-tentar agora piora o bloqueio.",
                      config.COOLDOWN_BLOQUEIO_HORAS)
            return
        marca = " (simulado)" if dry else ""
        log.info("Fim. Follows nesta execução%s: %d | dia: %d/%d.",
                 marca, guard.total_follows(), state.follows_ultimo_dia(), config.MAX_FOLLOWS_DIA)


def main():
    ap = argparse.ArgumentParser(description="auto-like-instagram")
    ap.add_argument("--login", action="store_true", help="login manual (1ª vez)")
    ap.add_argument("--dry-run", action="store_true", help="simula sem agir")
    ap.add_argument("--debug", action="store_true", help="dump da 1ª página de mensagens")
    ap.add_argument("--start-after", metavar="CODE", help="começar após este shortcode")
    ap.add_argument("--ignore-window", action="store_true", help="ignora janela de horário")
    ap.add_argument("--reset-cooldown", action="store_true", help="zera o cooldown de bloqueio")
    a = ap.parse_args()

    if a.reset_cooldown:
        State().limpar_cooldown()
        log.info("Cooldown zerado.")
        return
    if a.login:
        modo_login()
        return
    try:
        run(dry=a.dry_run, start_after=a.start_after, debug=a.debug,
            ignorar_janela=a.ignore_window)
    except BloqueioDetectado as e:
        log.error("⛔ Bloqueio: %s", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
