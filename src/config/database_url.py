"""Build the synchronous SQLAlchemy URL and pg8000 connection options."""

import ssl
import socket

from sqlalchemy.engine import make_url


def get_sync_database_config(connection_string):
    """Use pg8000 while preserving PostgreSQL URL credentials and TLS intent."""
    url = make_url(connection_string)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+pg8000")
    elif url.drivername == "postgresql+psycopg2":
        url = url.set(drivername="postgresql+pg8000")
    elif url.drivername != "postgresql+pg8000":
        raise ValueError("The synchronous database URL must use PostgreSQL")

    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    root_cert = query.pop("sslrootcert", None)
    if "sslcert" in query or "sslkey" in query:
        raise ValueError("Client certificate URL options are not supported")
    url = url.set(query=query)

    connect_args = {"tcp_keepalive": True}
    mode = str(sslmode).lower() if sslmode is not None else "prefer"
    if mode == "disable":
        connect_args["ssl_context"] = False
    elif mode == "require":
        if root_cert:
            context = ssl.create_default_context(cafile=root_cert)
            context.check_hostname = False
            connect_args["ssl_context"] = context
        else:
            # pg8000 documents True as TLS without certificate validation,
            # matching libpq sslmode=require when no root certificate is set.
            connect_args["ssl_context"] = True
    elif mode == "prefer":
        connect_args["ssl_context"] = None
    elif mode in ("verify-ca", "verify-full"):
        context = ssl.create_default_context(cafile=root_cert)
        if mode == "verify-ca":
            context.check_hostname = False
        connect_args["ssl_context"] = context
    else:
        raise ValueError("Unsupported PostgreSQL sslmode: {!r}".format(mode))
    return url, connect_args


def configure_pg8000_keepalive(dbapi_connection, _connection_record):
    """Set the per-socket keepalive timers required to outlive IPVS idle reap.

    pg8000 1.31.5 exposes the DBAPI connection's CoreConnection as ``_c`` and
    its active raw or SSL socket as ``_sock``. Fail closed if that pinned
    driver shape or any required socket option is unavailable.
    """
    core_connection = getattr(dbapi_connection, "_c", None)
    connection_socket = getattr(core_connection, "_sock", None)
    if connection_socket is None:
        raise RuntimeError("pg8000 1.31.5 active socket is unavailable")

    options = (
        (socket.SOL_SOCKET, "SO_KEEPALIVE", 1),
        (socket.IPPROTO_TCP, "TCP_KEEPIDLE", 30),
        (socket.IPPROTO_TCP, "TCP_KEEPINTVL", 10),
        (socket.IPPROTO_TCP, "TCP_KEEPCNT", 5),
    )
    for level, option_name, value in options:
        option = getattr(socket, option_name, None)
        if option is None:
            raise RuntimeError("Required TCP keepalive option is unavailable: " + option_name)
        try:
            connection_socket.setsockopt(level, option, value)
        except OSError as exc:
            raise RuntimeError("Could not configure TCP keepalive option: " + option_name) from exc
