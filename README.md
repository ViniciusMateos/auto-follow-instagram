# auto-like-instagram

> ⚠️ **Risco de bloqueio/ban.** Seguir em massa é a ação mais vigiada do Instagram.
> Isto viola os Termos de Uso e pode custar a conta. Comece conservador, rode
> `--dry-run` primeiro, e **respeite o kill-switch** (quando der "ação bloqueada",
> pare — insistir é o que vira ban permanente). Leia a seção [Ban & limites](#ban--limites).

Automatiza, na DM **vai toma no quase nada**, o fluxo que você fazia na mão:
acha o último post já marcado com ❤️ → pega o próximo post sem coração → abre,
lê as curtidas → segue todo mundo que curtiu (pulando quem já segue) → reage ❤️
no post pra marcar como feito → próximo.

Dirige um Chrome logado via **Playwright** e faz as chamadas de API de dentro da
própria página logada (mesmos cookies/fingerprint do navegador real). Os endpoints
foram extraídos de uma captura Fiddler real — ver [`API_REFERENCE.md`](API_REFERENCE.md).

## Setup

```bash
cd projetos/auto-like-instagram
python -m venv .venv && .venv\Scripts\activate     # opcional
pip install -r requirements.txt
python -m playwright install chromium
```

## Uso

```bash
# 1) login (só na 1ª vez) — abre o Chrome, você loga na mão, a sessão fica salva
python main.py --login

# 2) SIMULAÇÃO — lê de verdade, mostra qual post entraria e quantos seguiria, sem agir
python main.py --dry-run

# 3) rodar pra valer
python main.py

# variações
python main.py --start-after DQDF2QwkYh3   # força iniciar após um post específico
python main.py --debug                     # despeja a 1ª página de mensagens (calibração)
python main.py --ignore-window             # ignora a janela de horário
```

### Como ele decide qual post processar
**Marcador = QUALQUER reação de QUALQUER conta.** Se o post já tem alguma reação,
está feito. Ele varre a thread (de cima pra baixo, subindo a paginação) até achar
o primeiro post com reação — esse é o **boundary** — e processa os **próximos sem
nenhuma reação**, em ordem cronológica. Considera **todos os posts, de qualquer
remetente**. Ao terminar de seguir todos os curtidores de um post, ele reage ❤️
(o seu marcador) e vai pro próximo.

> Observação da thread real: os posts já feitos ficam fundo (≈página 8 da API,
> ~160 mensagens acima). A paginação sobe sozinha até o boundary e para.

### Quantos segue por post
**Todos.** Não há cap por post — segue todos os curtidores, depois reage e passa
pro próximo. Se um cap (quando ligado) interromper no meio, o post **não** é
marcado e retoma no próximo run, pulando quem já seguiu.

### ⚠️ Modo descoberta (padrão atual)
`APLICAR_CAPS = False` no `config.py`: roda **sem cap de follow e sem janela de
horário**, seguindo post após post **até o Instagram bloquear**. O kill-switch
continua ligado — quando o IG bloquear, ele **para o run na hora**, mostra a
mensagem do IG, registra o erro em `output/logs/` e imprime o saldo. Pra voltar ao
modo seguro (caps de 60/dia, 15/hora, janela 9–23h), é só `APLICAR_CAPS = True`.

## Configuração — `config.py`
Todos os limites e delays ficam lá:

| Parâmetro | Padrão | O quê |
|-----------|--------|-------|
| `APLICAR_CAPS` | False | False = modo descoberta (roda até bloquear); True = respeita os caps |
| `MAX_FOLLOWS_DIA` | 60 | teto por dia (só com `APLICAR_CAPS=True`) |
| `MAX_FOLLOWS_HORA` | 15 | teto por hora (só com `APLICAR_CAPS=True`) |
| `MAX_POSTS_POR_RUN` | 5 | posts por execução (só com `APLICAR_CAPS=True`) |
| `DELAY_FOLLOW` | 0–5 s | pausa aleatória entre follows |
| `DELAY_POST` | 3–8 min | pausa entre posts |
| `PAUSA_LONGA` | 5–15 s | respiro a cada 12 follows |
| `ACTIVE_HOURS` | 9–23 | só roda em horário "humano" (só com `APLICAR_CAPS=True`) |
| `SEGUIR_PRIVADOS` | True | segue privados também (vira pedido pendente) |

> Sem cap por post: segue **todos** os curtidores de cada post. Públicas viram
> "Seguindo" (`+` no log); privadas viram **pedido pendente** (`~` no log) e não
> contam no "Seguindo" do perfil.

## Saídas — `output/`
- `state.json` — usuários já seguidos, posts feitos, eventos de follow (caps).
- `run.log` — log de cada ação.
- `logs/erro_<data>.log` — traceback completo quando dá erro/bloqueio (console mostra só o resumido).
- `debug_messages.json` — só com `--debug`.

No fim de toda execução (inclusive em erro, bloqueio ou Ctrl+C) ele imprime o
**saldo**: seguidas (públicas), solicitadas (privadas) e puladas.

## Arquitetura
| Arquivo | Papel |
|---------|-------|
| `config.py` | limites, delays, alvo, sessão |
| `safety.py` | estado, caps rolantes, delays, janela, kill-switch |
| `ig.py` | Playwright + chamadas internas (mensagens, likers, follow, reação) |
| `main.py` | regra de retomada + loop de processamento + CLI |

## Ban & limites
Resumo do que segura (e do que detona) a conta:

- **Bloqueio temporário ("Ação bloqueada").** Punição mais comum, vem antes do ban.
  1ª vez: horas a 24–48h. Reincidência escala: 3 dias → 1 semana → 2 semanas →
  conta desativada / verificação de identidade. O script detecta os sinais
  (`feedback_required`, `checkpoint_required`, `spam`, HTTP 429, página HTML…) e
  **para o run na hora** — não fica insistindo (insistir é o que piora o bloqueio).
- **Limites realistas (conta antiga e aquecida):** ~100–150 follows/dia no teto
  absoluto; o seguro é começar com **50–80/dia**, ~10–20/hora, com intervalos
  aleatórios. Conta nova: bem menos.
- **O que dispara detecção:** velocidade sobre-humana, intervalos fixos, login
  programático (use sempre a sessão salva, nunca usuário/senha no código), IP de
  datacenter/VPN (use seu IP residencial normal), rodar 24/7.
- **Mitigações já embutidas:** caps diário/horário, delays aleatórios, janela de
  horário, pular já-seguidos/pendentes, kill-switch (para no bloqueio), estado persistente.

### Pontos que podem precisar de ajuste no 1º run real
Foram inferidos da captura e podem variar conforme o build do IG:
1. **Extração de tokens** (`fb_dtsg`/`lsd`/`av`) em `ig.py::JS_TOKENS` — se as
   chamadas GraphQL (mensagens/reação) derem erro, rode `--debug` e ajuste os regex.
2. **Detecção do ❤️** (`ig.py::tem_coracao`) — a forma exata do array `reactions`
   não apareceu populada na captura; o `output/debug_messages.json` mostra o real.
   O `state.json` local cobre a retomada mesmo que essa heurística falhe.
3. **Shortcode do post** sai do conteúdo da mensagem; posts privados/excluídos
   aparecem como "Mensagem indisponível" e são pulados (sem curtidas acessíveis).
