"""Production WSGI entrypoint (gunicorn wsgi:app -- see Procfile).

start_engine=False here on purpose: the trading engine runs as its own
dedicated process (`python manage.py run-engine`, the Procfile's
`worker` line), never inside a web worker. gunicorn commonly runs more
than one web worker process; each would otherwise start its own copy
of the engine thread -- multiple engines racing to place the same real
orders. See app/engine.py's run_forever docstring for the full
reasoning.
"""

from app import create_app

app = create_app(start_engine=False)
