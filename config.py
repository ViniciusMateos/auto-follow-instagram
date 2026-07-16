"""
Configuração do auto-like-instagram.

Tudo que controla comportamento e SEGURANÇA fica aqui. Os valores padrão são
conservadores de propósito — suba devagar, ao longo de dias, se nada bloquear.
Veja o README.md para a explicação de cada limite e o risco de ban.
"""
import os

# Raiz do projeto (onde este config.py mora). Todos os paths abaixo são ancorados
# aqui — assim NÃO importa de qual diretório você roda `python main.py`: o perfil
# logado, o output e o state.json são sempre os mesmos.
_BASE = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────── Alvo ───────────────────────────
GRUPO_NOME = "vaitomanoquasenada"
THREAD_ID = "24092553240433373"
THREAD_URL = f"https://www.instagram.com/direct/t/{THREAD_ID}/"

# ─────────────────────── Sessão / navegador ─────────────────
# Pasta com o perfil logado do Chrome (cookies/sessão persistem aqui).
# Na 1ª vez rode `python main.py --login` e faça login manual nessa janela.
USER_DATA_DIR = os.path.join(_BASE, "browser_profile")   # ancorado na pasta do projeto


# ─────────────────────── Proxy (opcional, configurável pelo app) ──────────
# Grava proxy.json {enabled, server, username, password}. Formato do Playwright.
def _carregar_proxy():
    import json
    f = os.path.join(_BASE, "proxy.json")
    if os.path.exists(f):
        try:
            d = json.load(open(f, encoding="utf-8"))
            if d.get("enabled") and d.get("server"):
                return {k: d[k] for k in ("server", "username", "password") if d.get(k)}
        except Exception:
            pass
    return None


PROXY = _carregar_proxy()


def _envbool(nome, padrao):
    v = os.environ.get(nome)
    return padrao if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# Default = PC (headed + Chrome real, menos detectável). No SERVIDOR headless (sem
# display), rode com IG_HEADLESS=1 e IG_CHROME_REAL=0 no ambiente (systemd .env).
HEADLESS = _envbool("IG_HEADLESS", False)            # headed é menos detectável (PC)
USAR_CHROME_REAL = _envbool("IG_CHROME_REAL", True)  # Chrome instalado (channel="chrome")
LOCALE = "pt-BR"
# Use o MESMO User-Agent do seu Chrome real (o da captura):
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# ───────────────── Constantes da API (da captura) ───────────
IG_APP_ID = "936619743392459"
ASBD_ID = "359341"

# ───────────────────── LIMITES DE SEGURANÇA ─────────────────
# MODO DESCOBERTA: com APLICAR_CAPS=False o script roda SEM cap de follow e SEM
# janela de horário — segue todo mundo, reage, vai pro próximo post, até o
# Instagram BLOQUEAR. O kill-switch continua ligado: ao bloquear, ele para e diz
# em quantos follows travou. ⚠️ É um teste de limite — você PODE levar bloqueio.
APLICAR_CAPS = False          # True = respeita os caps abaixo; False = roda até bloquear

MAX_FOLLOWS_DIA = 60          # (só vale com APLICAR_CAPS=True) teto por dia
MAX_FOLLOWS_HORA = 15         # (só vale com APLICAR_CAPS=True) teto por hora
MAX_POSTS_POR_RUN = 5         # (só vale com APLICAR_CAPS=True) posts por execução
# (sem cap por post: segue TODOS os curtidores; se bater cap no meio, o post NÃO é
#  marcado e retoma no próximo run, pulando quem já seguiu)

# Limite de follows REAIS (que viraram "Seguindo", sem contar pedido pendente) neste run.
# 0 = sem limite (vai até o IG bloquear). Vale INDEPENDENTE de APLICAR_CAPS.
LIMITE_FOLLOWS_RUN = 0

# Reservado (ainda NÃO lido no fluxo atual): espera entre execuções em sequência.
USAR_DELAY_ENTRE_CHATS = True
DELAY_ENTRE_CHATS = (120, 300)

# Delays aleatórios (segundos) — NUNCA use intervalo fixo
DELAY_FOLLOW = (0, 5)         # entre um follow e outro (modo rápido/descoberta)
DELAY_POST = (180, 480)       # entre terminar um post e abrir o próximo (3–8 min)
DELAY_ACAO_UI = (1.5, 4.0)    # micro-pausas entre cliques/navegações
PAUSA_LONGA_CADA = 12         # a cada N follows, faz uma pausa longa
PAUSA_LONGA = (5, 15)         # duração da pausa longa (reduzida p/ modo rápido)
#  -> p/ DESLIGAR de vez a pausa longa: PAUSA_LONGA_CADA = 999999

# Janela de horário "humano" (hora local 0–23). Fora disso, não roda.
ACTIVE_HOURS = (9, 23)

# Quem pular
PULAR_JA_SEGUIDOS = True      # friendship_status.following == True
PULAR_PENDENTES = True        # outgoing_request == True
SEGUIR_PRIVADOS = True        # segue privados também (vira "pedido pendente")

# Detecção de "post já processado":
# QUALQUER reação de QUALQUER conta = post já feito → pula pro próximo SEM reação.
# (post a processar = post compartilhado, de qualquer remetente, ainda sem nenhuma reação)
HEART_EMOJI = "❤"        # emoji que VOCÊ usa pra marcar o post como feito (U+2764)
# Se não houver NENHUM post reagido na varredura, começar do mais antigo?
START_FROM_OLDEST_SE_VAZIO = False   # False = não age sozinho; pede --start-after CODE

# Cooldown após sinal de bloqueio (horas)
COOLDOWN_BLOQUEIO_HORAS = 36

# Erros transitórios (5xx/HTML/vazio) são retentados; cada follow tenta N vezes.
# Se acontecerem MUITAS falhas SEGUIDAS (sem nenhum sucesso no meio), aí sim
# considera que a sessão caiu/checkpoint e para (com screenshot).
TENTATIVAS_POR_FOLLOW = 3
MAX_FALHAS_SEGUIDAS = 10

# Quantas páginas de mensagens varrer (20 msgs/página, newest→oldest).
# Os posts já feitos (com QUALQUER reação) ficam BEM no fundo — a paginação sobe até
# achar o primeiro post já reagido (o "boundary") e para. Este é só o teto de segurança.
MAX_PAGINAS_MENSAGENS = 14

# ─────────────────────────── Paths ──────────────────────────
OUTPUT_DIR = os.path.join(_BASE, "output")
STATE_FILE = os.path.join(OUTPUT_DIR, "state.json")
LOG_FILE = os.path.join(OUTPUT_DIR, "run.log")
