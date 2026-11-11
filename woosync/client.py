import base64
import json
import socket
import ssl
from urllib import error, parse, request

from django.conf import settings


class WooError(Exception):
    """A problem talking to the website. The message is safe to show: it never contains the keys."""


def _http_message(code):
    if code in (401, 403):
        return (
            f"The website refused the keys (HTTP {code}). Check WOO_KEY and WOO_SECRET, and that the key has Read/Write "
            "permission. If they are right, the host may be removing the login header: add WOO_QUERY_AUTH=1 to .env."
        )
    if code == 404:
        return ("The WooCommerce API was not found (HTTP 404). Check the website address, that WooCommerce is active, "
                "and that Settings > Permalinks is set to 'Post name'.")
    if code == 429:
        return "The website says there were too many requests. It will be tried again on the next sync."
    if code >= 500:
        return f"The website had an error (HTTP {code}). It will be tried again on the next sync."
    return f"The website refused the request (HTTP {code})."


class WooClient:
    MAX_PAGES = 200

    def __init__(self, url=None, key=None, secret=None, timeout=None, query_auth=None):
        cfg = getattr(settings, "WOOSYNC", None) or {}
        self.url = (cfg.get("URL", "") if url is None else url).strip().rstrip("/")
        self.key = cfg.get("KEY", "") if key is None else key
        self.secret = cfg.get("SECRET", "") if secret is None else secret
        self.timeout = timeout or cfg.get("TIMEOUT", 20)
        self.query_auth = cfg.get("QUERY_AUTH", False) if query_auth is None else query_auth

        if not (self.url and self.key and self.secret):
            raise WooError(
                "The website connection is not set up. Add WOO_URL, WOO_KEY and WOO_SECRET to the .env file, "
                "then restart the server."
            )
        if not self.url.lower().startswith("https://"):
            raise WooError("The website address must start with https://. The keys are never sent over plain http.")

    def _request(self, method, path, params=None, body=None):
        query = dict(params or {})
        headers = {"Accept": "application/json", "User-Agent": "Artyflakes-POS-sync/1.0"}
        if self.query_auth:
            query["consumer_key"] = self.key
            query["consumer_secret"] = self.secret
        else:
            token = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        url = f"{self.url}/wp-json/wc/v3/{path.lstrip('/')}"
        if query:
            url += "?" + parse.urlencode(query)
        req = request.Request(url, data=data, method=method, headers=headers)

        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                response_headers = resp.headers
        except error.HTTPError as exc:
            raise WooError(_http_message(exc.code)) from None
        except error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), ssl.SSLError):
                raise WooError(
                    "The website's security certificate could not be verified. Check the address and that the SSL "
                    "certificate is valid."
                ) from None
            raise WooError("The website could not be reached. Check the internet connection and the website address.") from None
        except (socket.timeout, TimeoutError):
            raise WooError("The website took too long to answer. It will be tried again on the next sync.") from None

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            raise WooError(
                "The website answered with something that is not WooCommerce data. Check the website address and "
                "that Permalinks is set to 'Post name'."
            ) from None
        return payload, response_headers

    def get_page(self, path, params=None):
        """One page. Returns (data, headers)."""
        return self._request("GET", path, params)

    def get_all(self, path, params=None):
        """Every page of a list, 100 at a time."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out, page = [], 1
        while page <= self.MAX_PAGES:
            params["page"] = page
            payload, headers = self._request("GET", path, params)
            if not isinstance(payload, list):
                raise WooError("The website answered with unexpected data.")
            out.extend(payload)
            try:
                total_pages = int(headers.get("X-WP-TotalPages", 1))
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages or not payload:
                break
            page += 1
        return out

    def post(self, path, body):
        payload, _ = self._request("POST", path, body=body)
        if not isinstance(payload, dict):
            raise WooError("The website answered with unexpected data.")
        return payload