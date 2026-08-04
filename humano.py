"""
Pausa humana intercalada no processo do bot.

Em vez de seguir/mandar DM em rajada mecânica, de vez em quando o bot SAI do processo,
vai pro feed, rola e curte umas coisas — como um usuário normal — e depois VOLTA. Isso
quebra o padrão de rajada, que é o que o IG mais detecta.

DE PROPÓSITO é LEVE (só feed + rolar + curtir): story/explore fazem `goto` pesado e, num
browser de sessão longa (run de 40min+), isso às vezes dá DEADLOCK — a página congela, o
worker fica mudo e o watchdog mata a run ('para do nada'). Feed + scroll + like dá o
disfarce sem esse risco. As ações do bot são via API, então navegar no meio não atrapalha.
"""
import random

from safety import log


def pausa_humana(ig, guard):
    """Paradinha humana leve: feed → rola e curte 1-2. Best-effort; se o feed engasgar, volta
    ao processo na hora (nunca trava/derruba a run)."""
    log.info("│    ~ pausa humana (dando uma navegada como gente)…")
    try:
        try:
            ig.ir("https://www.instagram.com/", timeout=25000)
        except Exception:
            log.info("│    ~ (feed engasgou, sigo o processo)")
            return
        curtidas = 0
        for _ in range(random.randint(3, 6)):
            if curtidas < 2 and random.random() < 0.45 and ig.curtir_visivel():
                curtidas += 1
                log.info("│      curti um post")
                guard.dormir((1.5, 4.0), "pos-curtir")
            else:
                ig.rolar()
                guard.dormir((2.5, 6.0), "lendo o feed")
        log.info("│    ~ voltando ao processo")
    except Exception as e:
        log.warning("pausa humana deu ruim (segue o baile): %s", str(e).splitlines()[0][:60])
