# Changelog

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
