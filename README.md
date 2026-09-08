# Copy Trading Platform

Um operador dispara Long/Short; toda conta seguidora (com a própria chave de API
real da Binance Futures, sem permissão de saque) espelha a operação proporcional
ao próprio saldo/leverage. O motor de execução roda no servidor, 24/7, independente
de qualquer navegador aberto -- ver
`docs/superpowers/specs/2026-09-07-copy-trading-platform-design.md` pro design
completo e o plano de implementação em `.claude/plans/` (sessão em que foi criado).

## Variáveis de ambiente obrigatórias

| Variável | Descrição |
|---|---|
| `FLASK_SECRET_KEY` | Chave de sessão do Flask. Gere com `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `DATABASE_URL` | String de conexão Postgres (ex: `postgresql://user:pass@host/db`). SQLite (`sqlite:///local.db`) funciona pra desenvolvimento local. |
| `ENCRYPTION_KEY` | Chave Fernet usada para criptografar toda API key/secret de seguidor. Gere com `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` **uma única vez** e guarde com segurança -- perder essa chave torna toda credencial salva irrecuperável. **Nunca** commitar no git. |

## Variáveis opcionais

| Variável | Padrão | Descrição |
|---|---|---|
| `BINANCE_TESTNET` | `true` | `true` = Binance Demo Trading (saldo fake). `false` = conta real (mainnet) -- só mude isso deliberadamente. |
| `ENGINE_TICK_SECONDS` | `5` | Intervalo do ciclo do motor de monitoramento/execução. |

## Rodando localmente

```bash
pip install -r requirements.txt
export FLASK_SECRET_KEY=... DATABASE_URL=sqlite:///local.db ENCRYPTION_KEY=... BINANCE_TESTNET=true
python manage.py create-owner --email voce@exemplo.com --password ...
python manage.py create-invite --by voce@exemplo.com
python manage.py runserver
```

## Testes

```bash
python -m unittest discover -p "test_*.py"
```

Só as funções puras (`app/engine.py`, `app/crypto.py`) têm teste automatizado --
o motor de ordens reais é validado manualmente contra a **testnet** da Binance
antes de qualquer chave real entrar em produção (ver seção "Verificação" da spec).

## Deploy (Render)

A aplicação roda como **dois processos separados**, não um só -- isso é
importante, não só uma escolha de organização:

- **`web`** (`gunicorn wsgi:app`) -- serve as páginas/rotas. Pode rodar com
  mais de uma worker sem problema.
- **`worker`** (`python manage.py run-engine`) -- o motor que monitora posições
  e manda ordem real. Roda como um único processo, sempre. **Nunca** rode o
  motor dentro do processo `web` em produção: se o `web` escalar pra mais de
  um worker (o padrão do Gunicorn), cada worker levantaria sua própria cópia
  do motor, e você teria múltiplas instâncias tentando abrir/fechar a MESMA
  ordem real ao mesmo tempo. `wsgi.py` já garante isso (`start_engine=False`)
  -- só não troque o comando de start do serviço web por outra coisa que
  chame `create_app()` sem esse parâmetro.

`render.yaml` já descreve os dois serviços + o banco Postgres. Passo a passo:

1. Subir este repositório pro GitHub (o Render se conecta a um repo git).
2. No Render, "New +" -> "Blueprint" -> conectar o repo -> ele lê o
   `render.yaml` e propõe criar `copy-trading-web`, `copy-trading-engine` e o
   banco `copy-trading-db` de uma vez.
3. **Antes de ativar**, definir manualmente (mesmo valor nos dois serviços,
   web e worker):
   - `FLASK_SECRET_KEY` -- gere com o comando da tabela acima.
   - `ENCRYPTION_KEY` -- gere com o comando da tabela acima. **Tem que ser
     idêntica nos dois serviços** -- é o `web` que criptografa a chave de
     cada seguidor ao salvar, e o `worker` que descriptografa pra montar a
     ordem real; com chaves diferentes, tudo que o `web` salvar vira lixo
     ilegível pro `worker`.
4. Confirmar que `copy-trading-engine` está num **plano pago** do tipo
   Background Worker -- o Render não oferece esse tipo de serviço no free
   tier, e é exatamente esse serviço que precisa ficar sempre ativo (ele não
   tem tráfego HTTP, então não tem o conceito de "dormir por inatividade"
   que o `web` tem -- mas também não roda de graça).
5. Depois do primeiro deploy, `python manage.py create-owner ...` uma vez
   (via o Shell do próprio serviço `web` no painel do Render) pra criar a
   primeira conta -- cadastro normal pelo site é só por convite.

## Segurança -- o que já está garantido, e o que ainda depende de você

- A API secret de cada conta é criptografada (Fernet) antes de ir pro banco; a
  chave de criptografia só existe como variável de ambiente do servidor.
- Nenhuma rota devolve uma secret decriptada pro navegador.
- Cadastro só por convite (sem cadastro público aberto).
- **Ainda depende de você**: confirmar com as pessoas que vão usar que a chave
  Binance delas tem só permissão de Futures (abrir/fechar posição), **sem
  permissão de saque** -- isso é configurado no painel da própria Binance, essa
  plataforma não consegue restringir isso por fora. Vale também considerar
  assessoria jurídica sobre regulação (CVM) antes de operar com contas de
  terceiros em produção.
