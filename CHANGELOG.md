# Changelog

## [1.0.1] — 2026-06-24

### Adicionado
- feat: saldo final em toda execução (seguidas / solicitadas / puladas), inclusive em erro, bloqueio ou Ctrl+C
- feat: traceback completo de erro/bloqueio vai pra `output/logs/` (console mostra só o resumido)
- feat: log em árvore por post (autor no topo, ações indentadas embaixo)
- fmt_tempo: esperas acima de 60s aparecem como `Xm Ys`

### Corrigido
- fix: paths (browser_profile/output/state.json) ancorados na pasta do projeto — não importa de qual diretório se roda
- fix: follow não quebra mais com resposta vazia/HTML do IG (trata como bloqueio, parada limpa)
- fix: log vai pro stdout com UTF-8 forçado — não aparece mais como "erro" vermelho no PowerShell nem bagunça emoji

### Modificado
- update: removido o cooldown automático por bloqueio — agora só para o run (você decide quando voltar)

## [1.0.0] — 2026-06-23

### Adicionado
- feat: automatizador que percorre o grupo de DM e segue os curtidores de cada post compartilhado
- Detecção de boundary por reação — qualquer reação de qualquer conta = post já processado
- Paginação que sobe sozinha no chat até achar o primeiro post reagido e para
- Segue **todos** os curtidores de cada post: públicas viram "Seguindo", privadas viram pedido pendente
- Reage ❤️ pra marcar o post como feito e segue pro próximo
- Caps de segurança (diário/horário), janela de horário e **modo descoberta** (roda sem cap até bloquear)
- Kill-switch que detecta bloqueio real do IG (`feedback_required`/`spam`/HTTP 429) e ativa cooldown
- Estado persistente retomável (`output/state.json`) e modo `--dry-run`
- Log em árvore por post: autor + contas seguidas / pedidos pendentes / pulados
- Comandos `--login`, `--debug`, `--start-after`, `--reset-cooldown`

### Documentação
- README com setup, fluxo, tabela de config e seção de risco de ban
- API_REFERENCE com endpoints reais (likers, follow, reação, lista de mensagens) extraídos de captura
- Prompt de referência do projeto
