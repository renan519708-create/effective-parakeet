# Copy Trading Platform — Design

## Contexto

Plataforma nova e separada do `gg-shot-monitor`, derivada do protótipo `backtest_lab.html`
(simulador client-side de Long/Short com stop-loss e redirecionamento de capital entre
símbolos). Objetivo: transformar isso numa ferramenta de **copy trading real** — um
operador dispara Long/Short e várias contas reais de terceiros (com a própria chave de
API da Binance Futures) seguem a operação, cada uma com seu próprio capital e risco.

Grupo de usuários: pequeno e conhecido (amigos/comunidade da pessoa dona do projeto),
não um produto público aberto a desconhecidos. Chaves de API são de conta **real**
(mainnet), sem permissão de saque — só abertura/fechamento de posição em Futures.

## Papéis

- **Owner** — a pessoa dona do projeto. Único papel que existe por padrão; promove
  contas a Operador; gera códigos de convite.
- **Operador** — o sócio (ou qualquer conta promovida pelo Owner). Dispara Long/Short/
  Encerrar/Reiniciar; define, por campanha: direção, universo de símbolos (símbolo
  único, Top N marketcap, ranks específicos, força/fraqueza vs BTC — mesmas opções do
  protótipo) e o stop-loss por operação (`stop_pct`). Não vê nem mexe na chave de
  ninguém.
- **Seguidor** — qualquer pessoa cadastrada por convite. Cadastra a própria chave de
  API (validada antes de salvar), define **uma vez** nas próprias configurações: % de
  risco por campanha (`risk_pct`, fatia do próprio saldo), leverage, drawdown máximo
  da própria carteira (`max_drawdown_pct` + liga/desliga), e o toggle "seguindo
  operações" (on/off). Essas configurações valem para qualquer campanha que a pessoa
  decida seguir. Vê só o próprio histórico e posições.

Divisão de responsabilidade: **parâmetros da estratégia** (direção, universo, stop
por operação) são do Operador, por campanha. **Parâmetros de risco pessoal**
(quanto do meu dinheiro, qual leverage, quando eu paro de vez) são de cada Seguidor,
configurados uma vez.

## Arquitetura

App Flask único, com três componentes lógicos no mesmo processo:

- **Web/API** — rotas de autenticação, cadastro por convite, painel do Operador,
  painel "Minha Conta" do Seguidor.
- **Motor de execução** — thread de background contínua (mesmo padrão do
  `background_loop` do `gg-shot-monitor`), rodando no servidor independente de
  qualquer navegador aberto. Ciclo a cada ~5 segundos (mais rápido que o
  `gg-shot-monitor`, porque aqui o stop-loss protege posição alavancada real de
  terceiros, não só sinal informativo).
- **Banco de dados** — Postgres gerenciado (Render/Railway).

**Segredos**: a API secret (e a key) de cada seguidor é criptografada com Fernet
(biblioteca `cryptography`) antes de gravar no banco. A chave de criptografia
(`ENCRYPTION_KEY`) fica só como variável de ambiente do servidor — nunca no banco,
nunca no repositório. Uma vez salva, a secret nunca mais é enviada de volta ao
navegador; o campo mostra só "•••• salva em DD/MM".

**Bootstrap do Owner**: como o cadastro é por convite (precisa de um Owner existente
pra gerar o primeiro código), a conta Owner inicial é criada por um comando de
gerência (`flask create-owner --email ... `), não pela tela de cadastro.

## Modelo de dados

- `users`: id, email (único), password_hash, role (owner/operator/follower),
  created_at
- `invite_codes`: id, code (único), created_by (user), used_by (user, nullable),
  created_at, used_at
- `api_credentials`: id, user_id (único), encrypted_api_key, encrypted_api_secret,
  is_valid, last_validated_at
- `follower_settings`: user_id, risk_pct (float, default 10.0), leverage (int,
  default 1), max_drawdown_enabled (bool), max_drawdown_pct (float, default 20.0),
  following_enabled (bool, default false)
- `campaigns`: id, direction (long/short), universe_scope, universe_params (JSON),
  stop_pct (float), status (active/stopped), started_by (operator), started_at,
  ended_at
- `campaign_symbols`: id, campaign_id, symbol, rank (nullable) — universo resolvido
  e fixado no início da campanha (não muda até Reiniciar, igual ao protótipo)
- `follower_allocations`: id, campaign_id, user_id, symbol, allocated_usd (float —
  o "livro" de capital por símbolo, equivalente ao `liveCapital` do protótipo),
  state (active/inactive), updated_at
- `positions`: id, campaign_id, user_id, symbol, side, entry_price (média, atualizada
  a cada ordem adicional), opened_at, status (open/closed), close_price, closed_at,
  close_reason (SL/Manual/Campanha-encerrada/Drawdown), realized_pnl_usd
- `order_log`: id, user_id, symbol, side, qty, order_type (open/add/close),
  binance_order_id, status (filled/failed), error_message, created_at — trilha de
  auditoria de toda ordem real tentada, com ou sem sucesso

## Fluxo do motor (a cada ciclo, ~5s, só quando há campanha ativa)

1. Busca o preço atual de cada símbolo do universo da campanha (uma vez, compartilhado
   entre todos os seguidores).
2. **Novo seguidor entrando na campanha** (following_enabled=true, chave válida, ainda
   sem alocação nessa campanha): calcula capital total = `risk_pct` × saldo real da
   conta agora; divide em partes iguais entre os símbolos do universo; abre uma ordem
   real por símbolo, no leverage configurado pela pessoa. O preço de entrada gravado
   é o preço real de execução devolvido pela Binance (`avgPrice` da ordem), nunca o
   preço teórico do sinal — mesma lição já aprendida no `gg-shot-monitor` (o preço
   teórico pode divergir do fill real por slippage). Se a pessoa desligar "seguindo
   operações" com posição real aberta numa campanha ativa, o motor fecha essa(s)
   posição(ões) real(is) dela no próximo ciclo (mesmo efeito do toggle por símbolo do
   protótipo, aqui na conta inteira) e ela só volta a abrir posição se ligar de novo.
3. **Checagem de stop, por posição aberta de cada conta**: se o preço adverso atingir
   `stop_pct` da campanha, fecha a posição real daquele símbolo/conta.
4. **Redirecionamento (fiel ao protótipo, adaptado pra ordem real)**: após um stop,
   localiza entre os OUTROS símbolos ainda ativos da MESMA conta o de melhor
   rendimento no momento; envia uma ordem real adicional nesse símbolo líder (mesmo
   lado/leverage), no valor do capital que sobrou do símbolo parado — a Binance
   recalcula automaticamente o preço médio de entrada da posição ao somar margem.
   Sem líder disponível, o capital fica livre na conta (não é usado até a próxima
   campanha).
5. **Drawdown máximo por conta**: se o valor total da carteira daquela conta (soma de
   `allocated_usd` + PnL não realizado) cair `max_drawdown_pct` desde o pico *daquela
   conta* nessa campanha, fecha todas as posições reais dela e marca a conta como
   inativa pro resto da campanha — não afeta as outras contas.
6. **Operador clica Encerrar operações**: fecha a posição real de toda conta seguidora
   ativa, campanha vira `stopped`.
7. **Operador clica Reiniciar**: zera alocações da campanha anterior (histórico de
   posições fechadas permanece no banco pra consulta), pronto pra nova campanha.

## Tratamento de erro

Toda falha (saldo insuficiente, chave revogada/permissão errada, LOT_SIZE/MIN_NOTIONAL
— reaproveitando a mesma checagem já validada no `gg-shot-monitor`) é isolada por
conta: fica registrada em `order_log`, aquela conta pausa sozinha com aviso visível no
painel dela, e o ciclo do motor continua normalmente pras demais. O ciclo inteiro roda
dentro de try/except (mesmo princípio do `background_loop` do `gg-shot-monitor`) —
uma conta ruim nunca derruba o monitoramento de todo mundo.

## Testes

Funções puras ganham teste unitário (mesmo padrão do `gg-shot-monitor`): cálculo de
tamanho de posição/redirecionamento, criptografia/decriptação (round-trip), checagem
de permissão por papel, cálculo de drawdown. O motor de ordens é validado contra
**testnet** (chave de teste, ambiente sandbox da Binance) antes de qualquer chave
real (inclusive a do sócio) ser exercida — isso é parte do processo de construção,
independente da decisão de já lançar com conta real pros seguidores.

## Hospedagem

PaaS (Render ou Railway) com Postgres gerenciado — HTTPS e deploy gerenciados,
disponibilidade 24/7 independente de qualquer computador pessoal ligado (crítico aqui:
o stop-loss de dinheiro real de terceiros precisa continuar rodando mesmo se o PC de
ninguém estiver ligado). Variáveis de ambiente: `DATABASE_URL`, `ENCRYPTION_KEY`,
`FLASK_SECRET_KEY`, credenciais de sessão.

## Fora de escopo (YAGNI, v1)

- Múltiplas campanhas simultâneas (só uma ativa por vez, igual ao protótipo).
- Leverage ou stop por símbolo individual (é por conta/campanha, não por símbolo).
- Qualquer permissão de saque ou movimentação de fundos além de abrir/fechar posição.
- Cadastro público aberto (fica só por convite nesta versão).
