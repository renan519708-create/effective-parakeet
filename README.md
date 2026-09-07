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

## Deploy (Render/Railway)

1. Provisionar um Postgres gerenciado -> copiar a `DATABASE_URL`.
2. Definir as 3 variáveis obrigatórias acima no painel do serviço.
3. **Escolher um plano que fique sempre ativo** (não "sleep" em inatividade) --
   o motor de monitoramento precisa continuar rodando 24/7 mesmo sem ninguém
   acessando o site, porque é ele que protege o stop-loss de dinheiro real.
4. Deploy via `Procfile` (`gunicorn "app:create_app()"`) -- o motor de fundo
   inicia automaticamente dentro do mesmo processo, ver `app/__init__.py`.
5. `python manage.py create-owner ...` uma vez, direto no ambiente de produção,
   pra criar a primeira conta (cadastro normal é só por convite).

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
