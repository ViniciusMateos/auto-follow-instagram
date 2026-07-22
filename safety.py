"""
Camada de segurança: estado persistente, limites (caps), delays humanos,
janela de horário e kill-switch de bloqueio.

Nada aqui fala com o Instagram — só guarda estado e decide se PODE agir.
"""
import json
import os
import sys
import time
import random
import logging
from datetime import datetime

import config


# ───────────────────────────── log ─────────────────────────────
def setup_logger():
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    # força UTF-8 no console (evita emoji bagunçado/erro no Windows/PowerShell)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logger = logging.getLogger("autofollow")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)   # stdout (não stderr) p/ não virar "erro" no PowerShell
    ch.setFormatter(fmt)
    # LOG POR SESSÃO: cada run grava seu PRÓPRIO arquivo em output/logs/run_<timestamp>.log,
    # pra você abrir DEPOIS e ver exatamente o que rolou naquele run (o run.log acumula TODOS
    # os runs e vira uma sopa ilegível). Mantém só os 30 mais recentes pra não encher o disco.
    _logs_dir = os.path.join(config.OUTPUT_DIR, "logs")
    os.makedirs(_logs_dir, exist_ok=True)
    _sess = logging.FileHandler(
        os.path.join(_logs_dir, "run_" + time.strftime("%Y%m%d_%H%M%S") + ".log"), encoding="utf-8")
    _sess.setFormatter(fmt)
    try:
        _antigos = sorted(f for f in os.listdir(_logs_dir)
                          if f.startswith("run_") and f.endswith(".log"))
        for _f in _antigos[:-30]:
            os.remove(os.path.join(_logs_dir, _f))
    except Exception:
        pass
    logger.addHandler(fh)
    logger.addHandler(_sess)
    logger.addHandler(ch)
    return logger


log = setup_logger()


def fmt_tempo(segundos):
    """Formata duração: <60s vira 'Xs'; acima disso, 'Xm Ys' (ou 'Xm' redondo)."""
    seg = int(round(segundos))
    if seg < 60:
        return f"{seg}s"
    m, s = divmod(seg, 60)
    return f"{m}m {s}s" if s else f"{m}m"


# ────────────────────────── exceções ───────────────────────────
class BloqueioDetectado(Exception):
    """Instagram sinalizou ação bloqueada / checkpoint / spam."""


class LimiteAtingido(Exception):
    """Bateu um cap (diário/horário) — parar com elegância, sem ser bloqueio."""


class ErroTransitorio(Exception):
    """Resposta recuperável (5xx/HTML/vazia) — dá pra tentar de novo, NÃO é bloqueio."""


# ─────────────────── detecção de bloqueio ───────────────────────
# Mensagens de bloqueio do IG vêm no campo "message" do JSON de resposta.
MENSAGENS_BLOQUEIO = {
    "feedback_required", "checkpoint_required", "challenge_required",
    "login_required", "consent_required", "rate_limit_error",
}

_STATUS = {
    200: "OK",
    400: "requisição recusada (validação ou ação bloqueada)",
    401: "não autenticado — a sessão expirou/caiu (reimporte os cookies)",
    403: "proibido — sessão inválida ou ação barrada",
    429: "RATE LIMIT — ações demais num intervalo curto; o IG está te limitando",
    500: "erro interno do servidor do IG (transitório)",
    502: "bad gateway no IG (transitório)",
    503: "serviço indisponível / sobrecarga no IG (transitório)",
    504: "timeout no servidor do IG (transitório)",
}


def explicar_status(code):
    """Tradução humana de um status HTTP (inclui os códigos não-padrão do Meta)."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "status desconhecido"
    if code in _STATUS:
        return _STATUS[code]
    if 560 <= code <= 599:
        return ("código não-padrão do Meta (faixa de throttle/sobrecarga) — quase sempre é "
                "o IG te SEGURANDO por excesso de ações (rate limit), não erro de dados")
    if 500 <= code < 600:
        return "erro no servidor do IG (5xx, geralmente transitório)"
    if 400 <= code < 500:
        return "requisição recusada pelo IG (4xx)"
    return "status inesperado"


def checar_bloqueio(status_code, texto):
    """Levanta BloqueioDetectado SÓ com sinal estruturado real de bloqueio.

    Importante: NÃO faz busca por substring no corpo inteiro — uma lista de
    curtidores legítima pode conter 'spam' no nome/username de alguém e dar
    falso positivo. Checa campos específicos do JSON de resposta.
    """
    texto = texto or ""
    if status_code == 429:
        raise BloqueioDetectado(f"HTTP 429 — {explicar_status(429)}.")

    # tenta parsear o JSON da resposta
    body = texto[len("for (;;);"):] if texto.startswith("for (;;);") else texto
    try:
        j = json.loads(body)
    except Exception:
        j = None

    if isinstance(j, dict):
        msg = str(j.get("message", "")).lower()
        status = str(j.get("status", "")).lower()
        fb = j.get("feedback_message") or j.get("feedback_title") or ""
        if msg in MENSAGENS_BLOQUEIO:
            extra = f' O IG disse: "{fb}"' if fb else ""
            raise BloqueioDetectado(f'ação bloqueada (message="{msg}").{extra}')
        if j.get("spam") is True:                       # flag explícita de spam do IG
            extra = f' O IG disse: "{fb}"' if fb else ""
            raise BloqueioDetectado(f"ação marcada como SPAM pelo IG.{extra}")
        if j.get("checkpoint_url") or j.get("challenge"):
            raise BloqueioDetectado("checkpoint/desafio — o IG quer que você confirme que é você no app.")
        # status=fail acompanhado de uma mensagem de bloqueio conhecida
        if status == "fail" and any(k in msg for k in ("feedback", "checkpoint", "challenge", "blocked")):
            raise BloqueioDetectado(f'ação falhou (message="{msg}").' + (f' O IG disse: "{fb}"' if fb else ""))
        return

    # Resposta não-JSON (HTML/vazia/5xx) NÃO é tratada como bloqueio aqui — é
    # transitória/recuperável. Quem chama tenta de novo (ver ErroTransitorio no ig.py).


# ───────────────────────── estado ──────────────────────────────
class State:
    def __init__(self, path=config.STATE_FILE):
        self.path = path
        self.data = {
            "followed_user_ids": [],
            "processed_post_codes": [],
            "processed_message_ids": [],
            "follow_events": [],          # epochs dos follows (para caps rolantes)
            "cooldown_until": 0,
        }
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception as e:
                log.warning("Não consegui ler state.json (%s); começando limpo.", e)

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)

    # --- sets de controle ---
    def ja_seguiu(self, uid):
        return str(uid) in self.data["followed_user_ids"]

    def marcar_seguido(self, uid):
        uid = str(uid)
        if uid not in self.data["followed_user_ids"]:
            self.data["followed_user_ids"].append(uid)
        self.data["follow_events"].append(int(time.time()))
        self.save()

    def post_processado(self, code, message_id):
        return (code in self.data["processed_post_codes"]
                or message_id in self.data["processed_message_ids"])

    def marcar_post(self, code, message_id):
        if code and code not in self.data["processed_post_codes"]:
            self.data["processed_post_codes"].append(code)
        if message_id and message_id not in self.data["processed_message_ids"]:
            self.data["processed_message_ids"].append(message_id)
        self.save()

    # --- caps rolantes ---
    def _prune(self):
        agora = int(time.time())
        self.data["follow_events"] = [t for t in self.data["follow_events"]
                                      if agora - t < 24 * 3600]

    def follows_ultima_hora(self):
        self._prune()
        agora = int(time.time())
        return sum(1 for t in self.data["follow_events"] if agora - t < 3600)

    def follows_ultimo_dia(self):
        self._prune()
        return len(self.data["follow_events"])

    # --- cooldown ---
    def em_cooldown(self):
        return time.time() < self.data.get("cooldown_until", 0)

    def cooldown_restante_h(self):
        return max(0, (self.data.get("cooldown_until", 0) - time.time()) / 3600)

    def ativar_cooldown(self, horas):
        self.data["cooldown_until"] = int(time.time() + horas * 3600)
        self.save()

    def limpar_cooldown(self):
        self.data["cooldown_until"] = 0
        self.save()


# ───────────────────── limites / delays ────────────────────────
class Guard:
    """Decide se pode agir e aplica as pausas humanas."""

    def __init__(self, state: State, dry_run=False):
        self.state = state
        self.dry_run = dry_run
        self._follows_no_run = 0
        self._dry_extra = 0          # follows simulados no dry-run (p/ caps fiéis)
        # saldo da execução (p/ o resumo final, mesmo em erro/bloqueio/Ctrl+C)
        self.seguidos = 0            # públicas que viraram "Seguindo"
        self.pendentes = 0           # privadas (pedido enviado)
        self.pulados = 0             # já seguia / privado(config) / etc.
        self.falhas_seguidas = 0     # erros transitórios consecutivos (reseta no sucesso)

    def checar_janela(self, ignorar=False):
        if ignorar or not config.APLICAR_CAPS or not getattr(config, "USAR_JANELA", False):
            return
        h = datetime.now().hour
        ini, fim = config.ACTIVE_HOURS
        if not (ini <= h < fim):
            raise LimiteAtingido(
                f"Fora da janela de horário ({ini}h–{fim}h). Hora atual: {h}h.")

    def checar_cooldown(self):
        if self.state.em_cooldown():
            raise LimiteAtingido(
                f"Em cooldown de bloqueio por mais {self.state.cooldown_restante_h():.1f}h.")

    def pode_seguir(self):
        """Levanta LimiteAtingido se algum cap foi batido (conta dry-run também)."""
        # limite de follows REAIS neste run — vale INDEPENDENTE dos caps diários/horários
        lim = getattr(config, "LIMITE_FOLLOWS_RUN", 0)
        if lim and self.seguidos >= lim:
            raise LimiteAtingido(f"Limite de {lim} follows reais neste run atingido.")
        if not config.APLICAR_CAPS:
            return                       # modo descoberta: sem cap de volume
        if self.state.follows_ultimo_dia() + self._dry_extra >= config.MAX_FOLLOWS_DIA:
            raise LimiteAtingido(f"Cap diário atingido ({config.MAX_FOLLOWS_DIA}).")
        if self.state.follows_ultima_hora() + self._dry_extra >= config.MAX_FOLLOWS_HORA:
            raise LimiteAtingido(f"Cap horário atingido ({config.MAX_FOLLOWS_HORA}).")

    def pos_follow(self):
        """Chamar após cada follow real: contabiliza e dorme."""
        self._follows_no_run += 1
        if self._follows_no_run % config.PAUSA_LONGA_CADA == 0:
            self.dormir(config.PAUSA_LONGA, "pausa longa")
        else:
            self.dormir(config.DELAY_FOLLOW, "entre follows")

    def pos_follow_dry(self):
        """Chamar após cada follow simulado: só contabiliza p/ o cap do dry-run."""
        self._dry_extra += 1

    def total_follows(self):
        """Follows feitos nesta execução (reais ou simulados)."""
        return self._dry_extra if self.dry_run else self._follows_no_run

    def dormir(self, faixa, motivo=""):
        a, b = faixa
        t = random.uniform(a, b)
        if self.dry_run:
            log.info("[dry-run] dormiria %s (%s)", fmt_tempo(t), motivo)
            return
        log.info("dormindo %s (%s)", fmt_tempo(t), motivo)
        time.sleep(t)
