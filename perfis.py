"""
Modularização do auto-follow: PERFIS (modos) e CHATS salvos.

- Um PERFIL ("modo") agrupa TODOS os knobs ajustáveis (tempos, caps, limites, quem
  pular). Vem com 3 modos prontos: `padrao` (os valores de hoje), `agressivo`, `calmo`.
- CHATS são as DMs salvas (nome + thread_id). Você escolhe qual rodar por nome —
  e pode salvar vários (histórico de chats).

Tudo persiste em JSON (`perfis.json` / `chats.json`) pro app/backend lerem e editarem.
Os DEFAULTS vivem aqui no código, então um clone novo já funciona sem configurar nada.
"""
import copy
import json
import os

_BASE = os.path.dirname(os.path.abspath(__file__))
PERFIS_FILE = os.path.join(_BASE, "perfis.json")
CHATS_FILE = os.path.join(_BASE, "chats.json")

# ─── knobs ajustáveis de um perfil (= os valores de HOJE, do config.py) ───
PERFIL_PADRAO = {
    "aplicar_caps": False,
    "max_follows_dia": 60,
    "max_follows_hora": 15,
    "max_posts_por_run": 5,
    "limite_follows_run": 0,          # 0 = sem limite (segue até o IG bloquear)
    "delay_follow": [0, 5],           # faixa aleatória (segundos)
    "delay_post": [180, 480],
    "delay_acao_ui": [1.5, 4.0],
    "pausa_longa_cada": 12,
    "pausa_longa": [5, 15],
    "usar_delay_entre_chats": True,   # toggle: esperar entre um chat e outro
    "delay_entre_chats": [120, 300],
    "active_hours": [9, 23],
    "pular_ja_seguidos": True,
    "pular_pendentes": True,
    "seguir_privados": True,
    "start_from_oldest_se_vazio": False,
}

# modos prontos — só listam o que MUDA em relação ao padrão
_MODOS_BUILTIN = {
    "padrao": {},                     # exatamente como você usa hoje
    "agressivo": {
        "aplicar_caps": False,
        "limite_follows_run": 0,      # sem freio, vai até bloquear
        "delay_follow": [0, 3],
        "delay_post": [60, 180],
        "pausa_longa_cada": 20,
        "pausa_longa": [3, 8],
        "usar_delay_entre_chats": False,
    },
    "calmo": {
        "aplicar_caps": True,
        "max_follows_dia": 60,
        "max_follows_hora": 15,
        "limite_follows_run": 100,
        "delay_follow": [4, 12],
        "delay_post": [300, 600],
        "pausa_longa_cada": 8,
        "pausa_longa": [20, 60],
        "usar_delay_entre_chats": True,
        "delay_entre_chats": [300, 600],
    },
}

# mapeia campo do perfil → atributo do módulo config (que o resto do código lê)
_MAP_CONFIG = {
    "aplicar_caps": "APLICAR_CAPS", "max_follows_dia": "MAX_FOLLOWS_DIA",
    "max_follows_hora": "MAX_FOLLOWS_HORA", "max_posts_por_run": "MAX_POSTS_POR_RUN",
    "limite_follows_run": "LIMITE_FOLLOWS_RUN", "delay_follow": "DELAY_FOLLOW",
    "delay_post": "DELAY_POST", "delay_acao_ui": "DELAY_ACAO_UI",
    "pausa_longa_cada": "PAUSA_LONGA_CADA", "pausa_longa": "PAUSA_LONGA",
    "usar_delay_entre_chats": "USAR_DELAY_ENTRE_CHATS",
    "delay_entre_chats": "DELAY_ENTRE_CHATS", "active_hours": "ACTIVE_HOURS",
    "pular_ja_seguidos": "PULAR_JA_SEGUIDOS", "pular_pendentes": "PULAR_PENDENTES",
    "seguir_privados": "SEGUIR_PRIVADOS",
    "start_from_oldest_se_vazio": "START_FROM_OLDEST_SE_VAZIO",
}


# ───────────────────────────── perfis ─────────────────────────────
def _default_perfis():
    out = {}
    for nome, override in _MODOS_BUILTIN.items():
        p = copy.deepcopy(PERFIL_PADRAO)
        p.update(override)
        out[nome] = p
    return out


def carregar_perfis():
    """Lê perfis.json (cria com os modos prontos se não existir). Completa campos
    faltantes com o padrão (compatibilidade ao adicionar knobs novos)."""
    if os.path.exists(PERFIS_FILE):
        try:
            with open(PERFIS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            for nome, p in list(d.items()):
                base = copy.deepcopy(PERFIL_PADRAO)
                base.update(p)
                d[nome] = base
            return d
        except Exception:
            pass
    d = _default_perfis()
    salvar_perfis(d)
    return d


def salvar_perfis(d):
    with open(PERFIS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def get_perfil(nome):
    return carregar_perfis().get(nome)


def salvar_perfil(nome, valores):
    """Cria/atualiza um modo (mescla com o padrão e grava)."""
    perfis = carregar_perfis()
    base = copy.deepcopy(PERFIL_PADRAO)
    base.update(valores or {})
    perfis[nome] = base
    salvar_perfis(perfis)
    return base


# ───────────────────────────── chats ─────────────────────────────
def _default_chats():
    # começa vazio de propósito: na 1ª vez o usuário configura o chat (nome + thread_id
    # ou @usuário) pelo app — nada de grupo "chumbado" que ele nunca escolheu.
    return []


def carregar_chats():
    if os.path.exists(CHATS_FILE):
        try:
            with open(CHATS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    c = _default_chats()
    salvar_chats(c)
    return c


def salvar_chats(c):
    with open(CHATS_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


def get_chat(nome_ou_id):
    alvo = str(nome_ou_id).strip().lower()
    for c in carregar_chats():
        if c.get("nome", "").strip().lower() == alvo or str(c.get("thread_id")) == str(nome_ou_id):
            return c
    return None


def add_chat(nome, thread_id):
    """Adiciona (ou renomeia, se o thread_id já existe) um chat salvo."""
    chats = carregar_chats()
    for c in chats:
        if str(c.get("thread_id")) == str(thread_id):
            c["nome"] = nome
            salvar_chats(chats)
            return c
    novo = {"nome": nome, "thread_id": str(thread_id)}
    chats.append(novo)
    salvar_chats(chats)
    return novo


def cachear_thread(nome, thread_id):
    """Salva o thread_id resolvido de volta no chat (salvo só pelo nome), pro próximo
    run não precisar procurar no inbox de novo."""
    chats = carregar_chats()
    mudou = False
    for c in chats:
        if c.get("nome", "").strip().lower() == str(nome).strip().lower() and not c.get("thread_id"):
            c["thread_id"] = str(thread_id)
            mudou = True
    if mudou:
        salvar_chats(chats)


# ─────────────────────── aplicar no runtime ───────────────────────
def aplicar(config, perfil, chat=None):
    """Sobrescreve os atributos do módulo `config` com os valores do perfil + chat.
    Como o resto do código lê `config.X`, isso aplica o modo/chat sem tocar em nada mais."""
    if chat:
        tid = str(chat.get("thread_id") or "")
        config.THREAD_ID = tid
        config.THREAD_URL = f"https://www.instagram.com/direct/t/{tid}/" if tid else ""
        config.GRUPO_NOME = chat.get("nome", config.GRUPO_NOME)
    for campo, attr in _MAP_CONFIG.items():
        if campo in perfil:
            v = perfil[campo]
            if isinstance(v, list) and len(v) == 2:   # faixas viram tupla
                v = tuple(v)
            setattr(config, attr, v)
