"""Request-scoped availability budget shared across agents and HTTP builders.

The server owns the limit. No session/model arguments can increase or reset it.
The lock also protects concurrent async tool calls delegated to worker threads.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Lock
from urllib.parse import urlsplit

AVAILABILITY_QUERY_LIMIT = 2
_current_budget = ContextVar("availability_query_budget", default=None)


class AvailabilityBudget:
    def __init__(self):
        self.used = 0
        self._lock = Lock()

    def consume(self):
        with self._lock:
            if self.used >= AVAILABILITY_QUERY_LIMIT:
                return False
            self.used += 1
            return True


    def next_response(self):
        with self._lock:
            self.used = 0


def complete_live_availability_response():
    """Only the server's completed live turn can replenish a live session."""
    budget = _current_budget.get()
    if budget is not None:
        budget.next_response()


@contextmanager
def availability_budget_scope(budget=None):
    existing = _current_budget.get()
    token = _current_budget.set(existing if existing is not None else budget or AvailabilityBudget())
    try:
        yield _current_budget.get()
    finally:
        _current_budget.reset(token)


def budgeted_response(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        with availability_budget_scope():
            return await func(*args, **kwargs)
    return wrapped


def budgeted_stream(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        budget = _current_budget.get() or AvailabilityBudget()
        iterator = func(*args, **kwargs)
        try:
            while True:
                try:
                    with availability_budget_scope(budget):
                        item = await anext(iterator)
                except StopAsyncIteration:
                    break
                # Do not leak a request's ContextVar into its stream consumer.
                yield item
        finally:
            with availability_budget_scope(budget):
                await iterator.aclose()
    return wrapped


def is_availability_http_tool(config):
    """Recognize the existing availability contract regardless of tool alias."""
    params = (config.get("parameters") or {}).get("body_params") or {}
    return (str(config.get("method", "")).upper() == "POST"
            and urlsplit(config.get("endpoint", "")).path.rstrip("/") == "/api/v1/search"
            and {"from", "to", "persons"}.issubset(params))


def consume_availability_query():
    budget = _current_budget.get()
    if budget is not None and budget.consume():
        return None
    code = ("AVAILABILITY_QUERY_LIMIT_REACHED" if budget is not None
            else "AVAILABILITY_BUDGET_UNAVAILABLE")
    return {
        "status": "error", "sucesso": False, "error": code, "codigo": code,
        "limite": AVAILABILITY_QUERY_LIMIT,
        "consultas_executadas": budget.used if budget is not None else 0,
        "busca_parcial": True,
        "message": ("Não execute outras consultas nesta resposta. Apresente somente os resultados "
                    "já verificados como pesquisa parcial, preservando os requisitos do cliente. "
                    "Não declare indisponibilidade global nem peça reconfirmação de dados claros. "
                    "Se o pedido de várias unidades continua incompleto, siga a política de composição "
                    "assistida e encaminhamento permitida para este agente. Não peça autorização genérica "
                    "para continuar nem anuncie encaminhamento sem sucesso da ferramenta. Se o cliente "
                    "solicitar novas buscas em outro turno, haverá novo orçamento."),
    }
