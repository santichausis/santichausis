"""Cliente mínimo de la API de GitHub (solo stdlib), con reintentos y timeout.

Reintenta errores transitorios (5xx, 429, límite secundario con Retry-After, red caída,
timeouts, respuestas cortadas). No reintenta 4xx "de verdad" (404, 401, 422...).
"""

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TIMEOUT = 30
MAX_ATTEMPTS = 4
MAX_BACKOFF = 30
RETRY_STATUS = {429, 500, 502, 503, 504}

# Todo lo que puede salir mal hablando con la API; los callers capturan esto, no Exception.
API_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError,
              http.client.HTTPException, json.JSONDecodeError)

_sleep = time.sleep  # reemplazable en tests


def _retry_delay(attempt, err):
    """Segundos a esperar antes del próximo intento, o None si no vale la pena reintentar."""
    backoff = min(2 ** (attempt - 1), MAX_BACKOFF)
    if isinstance(err, urllib.error.HTTPError):
        retry_after = err.headers.get("Retry-After") if err.headers else None
        if err.code in (403, 429) and retry_after is not None:  # límite secundario de rate
            if retry_after.isdigit() and int(retry_after) <= 60:
                return max(int(retry_after), 1)
            return None
        return backoff if err.code in RETRY_STATUS else None
    return backoff  # red / timeout / respuesta cortada


def _open_json(req):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except API_ERRORS as e:
            delay = _retry_delay(attempt, e)
            if delay is None or attempt == MAX_ATTEMPTS:
                raise
            _sleep(delay)


def _auth_headers():
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def api(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _open_json(urllib.request.Request(url, headers=_auth_headers()))


def graphql(query, variables):
    if not os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError("GraphQL needs GITHUB_TOKEN")
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    data = _open_json(urllib.request.Request(API + "/graphql", data=body, headers=headers))
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


def paginate(path, params=None, max_pages=5):
    """Junta páginas de un endpoint REST que devuelve lista (hasta max_pages de 100)."""
    out = []
    for page in range(1, max_pages + 1):
        batch = api(path, {**(params or {}), "per_page": 100, "page": page})
        out.extend(batch)
        if len(batch) < 100:
            break
    return out
