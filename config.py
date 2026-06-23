"""
Configuração do auto-like-instagram.

Tudo que controla comportamento e SEGURANÇA fica aqui. Os valores padrão são
conservadores de propósito — suba devagar, ao longo de dias, se nada bloquear.
Veja o README.md para a explicação de cada limite e o risco de ban.
"""

# ─────────────────────────── Alvo ───────────────────────────
GRUPO_NOME = "vai toma no quase nada"
THREAD_ID = "24092553240433373"
THREAD_URL = f"https://www.instagram.com/direct/t/{THREAD_ID}/"

# ─────────────────────── Sessão / navegador ─────────────────
# Pasta com o perfil logado do Chrome (cookies/sessão persistem aqui).
# Na 1ª vez rode `python main.py --login` e faça login manual nessa janela.
USER_DATA_DIR = "browser_profile"      # criado dentro da pasta do projeto
HEADLESS = False                       # headed é menos detectável; mude por conta própria
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

# Quantas páginas de mensagens varrer (20 msgs/página, newest→oldest).
# Os posts já feitos (com ❤️) ficam BEM no fundo — a paginação sobe até achar
# o seu primeiro coração (o "boundary") e para. Este é só o teto de segurança.
MAX_PAGINAS_MENSAGENS = 14

# ─────────────────────────── Paths ──────────────────────────
OUTPUT_DIR = "output"
STATE_FILE = "output/state.json"
LOG_FILE = "output/run.log"
