# Aba "Resultados" — Design

## Contexto

Hoje não existe nenhum lugar no app para acompanhar desempenho ao longo do
tempo — só o estado atual (posições abertas, histórico bruto de posições
fechadas em "Minha conta", e o "Resultado" por campanha em "Últimas
campanhas"). Este design adiciona uma aba "Resultados" com duas visões:

1. **Meus resultados** — cada usuário logado (seguidor ou operador) vê o
   desempenho da própria conta, baseado nas suas posições reais fechadas.
2. **Histórico da estratégia** — qualquer usuário logado vê o desempenho
   agregado da estratégia em si (por campanha/símbolo), sem depender de
   nenhuma conta específica. Serve como material de prova para convencer
   um seguidor novo de que o sistema funciona, sem expor saldo/PnL real de
   ninguém.

Ambas as visões compartilham o mesmo formato: filtro de janela de tempo →
gráfico de linha única (progresso acumulado) → tabela de operações.

## Fora de escopo

- Página pública (sem login) — decidido explicitamente que fica restrita a
  usuários logados.
- PnL em dólares na visão "Histórico da estratégia" — só percentual.
- Gráfico interativo (tooltip ao passar o mouse, zoom) — SVG estático por
  ora; Chart.js fica como upgrade futuro se fizer falta.
- Qualquer alteração em como campanhas/posições são criadas ou fechadas —
  este design é só leitura/relatório sobre dados que já existem.

## Navegação

Um item novo "Resultados" no menu do topo (`base.html`), ao lado de "Minha
conta", visível para qualquer usuário autenticado (não depende de
`can_operate`). Rota `GET /resultados`, blueprint novo `results_bp`
(`app/results.py`), registrado em `app/__init__.py` junto dos demais.

Dentro da página: um alternador "Meus resultados" / "Histórico da
estratégia" (querystring `?view=mine|strategy`, default `mine`) e os
botões de janela de tempo Diário/Semanal/Mensal/Anual/Acumulado
(querystring `?period=daily|weekly|monthly|yearly|all`, default `all`).
Cada clique é um novo GET com querystring — sem JS de estado no cliente,
consistente com o resto do app (server-rendered, sem build step).

## Janelas de tempo

Janelas rolantes a partir de agora, não alinhadas a calendário:

| period    | corte                  |
|-----------|------------------------|
| `daily`   | últimas 24h            |
| `weekly`  | últimos 7 dias         |
| `monthly` | últimos 30 dias        |
| `yearly`  | últimos 365 dias       |
| `all`     | sem corte (tudo)       |

Cada janela **reinicia o acumulado do zero** no início do próprio corte —
"Mensal" mostra o progresso só dos últimos 30 dias, não carrega o que
veio antes. Isso evita que um período curto fique sempre "afogado" pelo
total histórico.

## Cálculo do resultado por operação

Reaproveita `price_roi_pct(direction, entry_price, exit_price, leverage=1)`
já existente em `app/engine.py` — mesmo critério usado em todo o resto do
dashboard (operator, campaign_result_pct): percentual de movimento de
preço, direção-consciente, **sem alavancagem** (mantém as duas visões
comparáveis entre si e com o resto do app).

- **Meus resultados**: uma linha por `Position` com `status="closed"` do
  usuário logado — `entry=entry_price`, `exit=close_price`,
  `direction=side`, `at=closed_at`.
- **Histórico da estratégia**: uma linha por `CampaignSymbol` de toda
  `Campaign` com `status="stopped"`, filtrando as que têm
  `entry_price` E `exit_price` preenchidos (mesma regra de "evidência real
  de operação" já usada em `campaign_result_pct`/`Ultimas campanhas` —
  symbols sem posição real nunca entram aqui) — `entry=entry_price`,
  `exit=exit_price`, `direction=Campaign.direction`, `at=Campaign.ended_at`
  (todos os símbolos da mesma campanha compartilham esse instante).

Em ambos os casos, `trades` chega em `build_result_series` já ordenada por
`at` crescente e, em caso de empate (vários símbolos da mesma campanha
fechando no mesmo `at`), por `symbol` alfabético — só para desempate
determinístico, a ordem entre símbolos empatados não muda o resultado
acumulado final.

## Módulo novo: `app/results.py`

Funções puras (testáveis sem DB, recebem listas de tuplas/dicts já
carregadas):

- `PERIOD_CUTOFFS = {"daily": timedelta(hours=24), "weekly": timedelta(days=7), "monthly": timedelta(days=30), "yearly": timedelta(days=365), "all": None}`
- `build_result_series(trades, now)` — `trades`: lista de
  `{symbol, entry, exit, direction, at}` já ordenada por `at` crescente.
  Retorna `{"points": [{"t": iso, "cum_pct": float}], "rows": [{"symbol", "entry", "exit", "pct", "at"}]}`
  com `cum_pct` sendo a soma corrida de `price_roi_pct(...)` — pura,
  sem tocar em `db`.
- `render_line_svg(points, width=680, height=200)` — gera o `<svg>` do
  gráfico (polyline + linha pontilhada no zero), string pronta pra
  `| safe` no Jinja. Verde/vermelho conforme o valor final acumulado seja
  positivo/negativo (reaproveita a mesma paleta de `pnl-pos`/`pnl-neg`
  em hex, já que é SVG puro sem acesso às variáveis CSS do tema).

`app/results.py` faz as queries (filtra por período, monta `trades`), chama
essas duas funções puras, e devolve o contexto pro template — mantendo a
mesma separação "lógica pura testável" vs "camada de I/O" já usada em
`app/engine.py`.

## Rota (`app/results.py`)

```
@results_bp.route("/")
@login_required
def dashboard():
    view = request.args.get("view", "mine")
    period = request.args.get("period", "all")
    trades = _mine_trades(current_user.id, period) if view == "mine" else _strategy_trades(period)
    series = build_result_series(trades, datetime.now(timezone.utc))
    chart_svg = render_line_svg(series["points"])
    return render_template("results.html", view=view, period=period, rows=series["rows"], chart_svg=chart_svg)
```

Sem posições/campanhas na janela selecionada → sem quebrar: `points`/`rows`
vazios, template mostra um estado vazio ("Nenhuma operação nesse período")
em vez do SVG.

## Template (`app/templates/results.html`)

Segue o mesmo padrão visual de `operator_dashboard.html`/`follower_dashboard.html`
(`.panel`, `.table-wrap`, `.pnl-pos`/`.pnl-neg`): um `.panel` com o
alternador (dois links/botões que trocam `?view=`), um `.panel` com os 5
botões de período (trocam `?period=`, mantendo o `view` atual), o SVG do
gráfico, e a tabela de operações reaproveitando o mesmo estilo de tabela já
usado no resto do app.

## Testes

`test_results.py` cobre `build_result_series` (soma corrida correta, sinal
por direção, lista vazia não quebra) e `render_line_svg` (gera string SVG
válida para 0, 1, e N pontos) — mesmo espírito de `test_engine.py`.
