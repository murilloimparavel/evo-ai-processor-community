"""Explicit, read-only custom HTTP execution for the isolated runtime.

A human-reviewed policy binds the whole database tool configuration, purpose and
network destination. Legacy HTTP closures are never executed here. Public DNS
is validated and pinned for each connection; internal CIDRs require a separate
operator-owned origin allowlist. No financial mutations are supported.
"""
import asyncio
import hashlib
import inspect
import keyword
import ipaddress
import json
import os
import re
import socket
import ssl
from string import Formatter
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from google.adk.tools import FunctionTool

FIELDS = ("name", "description", "method", "endpoint", "headers", "path_params", "query_params", "body_params", "values", "error_handling")
PURPOSES = {"availability", "reservation_lookup", "payment_status", "date_now", "date_calculation", "property_lookup"}
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization", "proxy-connection", "x-knowledge-grant", "x-knowledge-executor", "forwarded", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-for", "x-original-url", "x-rewrite-url", "upgrade", "te", "accept-encoding"}
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
MAX_BYTES = 262144


class CustomDenied(PermissionError):
    def __init__(self):
        super().__init__("custom tool unavailable")


class StrictCustomFunctionTool(FunctionTool):
    """ADK normally drops extra arguments; reject them before normalization."""
    def __init__(self, func, consume_attempt):
        super().__init__(func=func)
        self._consume_attempt = consume_attempt

    async def run_async(self, *, args, tool_context):
        self._consume_attempt()
        valid = set(inspect.signature(self.func).parameters)
        if not isinstance(args, dict) or set(args) - valid:
            raise CustomDenied()
        return await super().run_async(args=args, tool_context=tool_context)


def config_dict(record):
    return {field: getattr(record, field, None) for field in FIELDS}


def fingerprint_config(record):
    return hashlib.sha256(json.dumps(config_dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _origin(parts):
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise CustomDenied()
    try:
        port = parts.port
    except ValueError:
        raise CustomDenied() from None
    host = parts.hostname.lower()
    if any(c in host for c in "{}%\\") or not host.isascii():
        raise CustomDenied()
    authority = f"[{host}]" if ":" in host else host
    return "https://" + authority + (f":{port}" if port and port != 443 else "")


def validate_policy(record, policy):
    if os.getenv("KNOWLEDGE_CUSTOM_TOOLS_ENABLED") != "true" or not isinstance(policy, dict):
        raise CustomDenied()
    if policy.get("purpose") not in PURPOSES or policy.get("effect") != "read":
        raise CustomDenied()
    if str(getattr(record, "id", "")) != policy.get("tool_id") or fingerprint_config(record) != policy.get("fingerprint"):
        raise CustomDenied()
    method = getattr(record, "method", None)
    if method not in {"GET", "POST"} or policy.get("method") != method:
        raise CustomDenied()
    parts = urlsplit(record.endpoint)
    if (parts.query or parts.fragment or "\\" in parts.path or "%" in parts.path or
            any(p in {".", ".."} for p in parts.path.split("/")) or not parts.path.startswith("/") or
            parts.path != policy.get("path") or _origin(parts) != policy.get("origin")):
        raise CustomDenied()
    projection = policy.get("response_fields")
    if (not isinstance(projection, list) or not 1 <= len(projection) <= 40 or
            any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in projection)):
        raise CustomDenied()
    headers = record.headers or {}
    if not isinstance(headers, dict) or len(headers) > 30:
        raise CustomDenied()
    if len({key.lower() for key in headers if isinstance(key, str)}) != len(headers):
        raise CustomDenied()
    for key, value in headers.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9-]+", key) or key.lower() in FORBIDDEN_HEADERS or
                not isinstance(value, str) or len(value) > 4096 or any(c in value for c in "{}\r\n")):
            raise CustomDenied()
    if len(json.dumps(headers).encode()) > 16384:
        raise CustomDenied()
    if method == "GET" and record.body_params:
        raise CustomDenied()
    parameters(record, policy)  # reject malformed/ambiguous schemas before exposing tool
    return parts


def parameters(record, policy):
    specs = {}
    context = policy.get("context_params", {})
    if (not isinstance(context, dict) or any(v not in {"conversation_id", "contact_id", "event_id"} for v in context.values())):
        raise CustomDenied()
    raw_defaults = record.values or {}
    if not isinstance(raw_defaults, dict):
        raise CustomDenied()
    defaults = {key: value for key, value in raw_defaults.items() if key not in {"__evo_modes_meta__", "__modes_meta__"}}
    for location in ("path_params", "query_params", "body_params"):
        definitions = getattr(record, location, None) or {}
        if not isinstance(definitions, dict):
            raise CustomDenied()
        for key, definition in definitions.items():
            if not isinstance(key, str) or not key.isidentifier() or key in specs:
                raise CustomDenied()
            typed = isinstance(definition, dict) and "type" in definition
            if typed and definition["type"] not in TYPES:
                raise CustomDenied()
            specs[key] = (location, definition, typed)
    if set(context) - set(specs) or set(defaults) - set(specs):
        raise CustomDenied()
    try:
        fields = [field for _, field, spec, conversion in Formatter().parse(urlsplit(record.endpoint).path) if field is not None]
    except ValueError:
        raise CustomDenied() from None
    if any(not field.isidentifier() or field not in specs or specs[field][0] != "path_params" for field in fields):
        raise CustomDenied()
    if set(fields) != {key for key, value in specs.items() if value[0] == "path_params"}:
        raise CustomDenied()
    return specs


def aliases(record, policy):
    specs = parameters(record, policy)
    result = {}
    for key, (_, _, typed) in specs.items():
        if not typed or key in policy.get("context_params", {}):
            continue
        safe = key + "_" if keyword.iskeyword(key) else key
        if safe in result:
            raise CustomDenied()
        result[safe] = key
    return result


def signature(record, policy):
    args = []
    mapping = {value: key for key, value in aliases(record, policy).items()}
    for key, (_, definition, typed) in parameters(record, policy).items():
        if not typed or key in policy.get("context_params", {}):
            continue
        required = definition.get("required") is True
        default = inspect.Parameter.empty if required else definition.get("default")
        annotation = TYPES[definition["type"]]
        if definition["type"] == "number":
            annotation = float
        args.append(inspect.Parameter(mapping[key], inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation))
    return inspect.Signature(args, return_annotation=dict)


def request_data(record, policy, args, grant):
    specs = parameters(record, policy)
    mapping = aliases(record, policy)
    if not isinstance(args, dict) or set(args) - set(mapping):
        raise CustomDenied()
    values = {key: value for key, value in (record.values or {}).items() if key not in {"__evo_modes_meta__", "__modes_meta__"}}
    values.update({mapping[key]: value for key, value in args.items()})
    for key, field in policy.get("context_params", {}).items():
        values[key] = getattr(grant, field)
    data = {"path_params": {}, "query_params": {}, "body_params": {}}
    for key, (location, definition, typed) in specs.items():
        if typed:
            value = values.get(key, definition.get("default"))
            if value is None:
                if definition.get("required") is True:
                    raise CustomDenied()
                continue
            expected = TYPES[definition["type"]]
            if not isinstance(value, expected) or (isinstance(value, bool) and definition["type"] in {"integer", "number"}):
                raise CustomDenied()
            if "enum" in definition and value not in definition["enum"]:
                raise CustomDenied()
            if isinstance(value, str) and (len(value) > min(int(definition.get("maxLength", 2048)), 2048) or any(c in value for c in "\x00\r\n")):
                raise CustomDenied()
        else:
            value = definition
        data[location][key] = value
    path = urlsplit(record.endpoint).path
    for key, value in data["path_params"].items():
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", str(value)) or str(value) in {".", ".."}:
            raise CustomDenied()
        path = path.replace("{" + key + "}", quote(str(value), safe=""))
    if "{" in path or "}" in path:
        raise CustomDenied()
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    if len(payload) > 16384:
        raise CustomDenied()
    return path, data["query_params"], data["body_params"]


async def pinned_address(parts):
    origin = _origin(parts)
    try:
        configured = json.loads(os.getenv("KNOWLEDGE_CUSTOM_EGRESS_NETWORKS", "{}"))
        if not isinstance(configured, dict):
            raise CustomDenied()
        cidrs = configured.get(origin, [])
        if not isinstance(cidrs, list):
            raise CustomDenied()
        networks = [ipaddress.ip_network(item) for item in cidrs]
        results = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM), timeout=3)
        addresses = sorted({item[4][0] for item in results})
        if not addresses:
            raise CustomDenied()
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if address.is_multicast or address.is_unspecified or getattr(address, "ipv4_mapped", None) is not None:
                raise CustomDenied()
            if not (any(address in network for network in networks) if origin in configured else address.is_global and not address.is_reserved):
                raise CustomDenied()
        return addresses[0]
    except (ValueError, TypeError, OSError, asyncio.TimeoutError):
        raise CustomDenied() from None


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if len(secret) >= 4:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    if isinstance(value, dict):
        # Never rewrite structural keys: doing so can collide with another key
        # and alter payment/status semantics. A reflected credential in a key
        # denies this response entirely instead of leaking or inventing data.
        if any(_redact(key, secrets) != key for key in value):
            raise CustomDenied()
        return {key: _redact(v, secrets) for key, v in value.items()}
    return value


async def execute(record, policy, args, grant, reauthorize):
    """Execute one explicitly authorized read with fresh DB state and pinned TLS."""
    parts = validate_policy(record, policy)
    path, query, body = request_data(record, policy, args, grant)
    address = await pinned_address(parts)
    await reauthorize()  # DNS lookup cannot extend a revoked capability/grant
    ip_host = f"[{address}]" if ":" in address else address
    ip_url = f"https://{ip_host}:{parts.port or 443}{path}"
    headers = {key.lower(): value for key, value in (record.headers or {}).items()}
    headers["host"] = parts.netloc
    headers["accept"] = "application/json"
    headers["accept-encoding"] = "identity"
    secrets = []
    for key, value in headers.items():
        if key.lower() not in {"host", "accept", "content-type"}:
            secrets.extend([value, value.split(" ")[-1]])
    for key, value in (record.values or {}).items():
        if isinstance(value, str) and re.search(r"token|secret|password|api.?key|authorization", key, re.I):
            secrets.append(value)
    try:
        tls = ssl.create_default_context(cafile=os.getenv("SSL_CERT_FILE") or None)
        # Use the transport directly: AsyncClient logs full request URLs at
        # INFO, which would persist reservation/partner codes from paths/query.
        # No redirect/proxy layer is present, and TLS still verifies the original
        # hostname through explicit SNI while the TCP destination is pinned.
        async with httpx.AsyncHTTPTransport(verify=tls, retries=0) as transport:
            request = httpx.Request(record.method, ip_url, headers=headers, params=query,
                json=body if record.method == "POST" else None,
                extensions={"sni_hostname": parts.hostname,
                            "timeout": {"connect": 3.0, "read": 8.0, "write": 3.0, "pool": 3.0}})
            response = await transport.handle_async_request(request)
            try:
                if (response.status_code != 200 or "application/json" not in response.headers.get("content-type", "").lower()
                        or response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}):
                    raise CustomDenied()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise CustomDenied()
                    chunks.append(chunk)
            finally:
                await response.aclose()
        result = json.loads(b"".join(chunks), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
        if not isinstance(result, dict):
            raise CustomDenied()
        projected = {key: result[key] for key in policy["response_fields"] if key in result}
        if not projected:
            raise CustomDenied()
        return {"status": "success", "purpose": policy["purpose"], "volatile": True, "result": _redact(projected, secrets)}
    except (httpx.HTTPError, ValueError, TypeError, RecursionError, OSError):
        raise CustomDenied() from None
