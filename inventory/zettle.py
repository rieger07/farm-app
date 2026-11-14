"""Thin Zettle API client (OAuth code flow, products, purchases)."""

import os
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://oauth.zettle.com/authorize"
TOKEN_URL = "https://oauth.zettle.com/token"
PRODUCTS_URL = "https://products.izettle.com/organizations/self/products/v2"
PURCHASES_URL = "https://purchase.izettle.com/purchases/v2"
SCOPES = "READ:PURCHASE READ:PRODUCT"


class ZettleError(Exception):
    pass


def client_id() -> str:
    return os.environ.get("ZETTLE_CLIENT_ID", "")


def client_secret() -> str:
    return os.environ.get("ZETTLE_CLIENT_SECRET", "")


def configured() -> bool:
    return bool(client_id() and client_secret())


def build_authorize_url(redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id(),
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
        }
    )


def _token_request(data: dict, http: httpx.Client | None) -> dict:
    payload = {"client_id": client_id(), "client_secret": client_secret(), **data}
    own = http is None
    http = http or httpx.Client(timeout=20)
    try:
        resp = http.post(TOKEN_URL, data=payload)
    finally:
        if own:
            http.close()
    if resp.status_code != 200:
        raise ZettleError(f"Token request failed ({resp.status_code}).")
    return resp.json()


def exchange_code(code: str, redirect_uri: str, http: httpx.Client | None = None) -> dict:
    return _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        http,
    )


def refresh_access_token(refresh_token: str, http: httpx.Client | None = None) -> dict:
    return _token_request(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}, http
    )


class ZettleClient:
    def __init__(self, access_token: str, http: httpx.Client | None = None):
        self._http = http or httpx.Client(timeout=30)
        self._headers = {"Authorization": f"Bearer {access_token}"}

    def _get(self, url: str, params: dict | None = None):
        resp = self._http.get(url, headers=self._headers, params=params)
        if resp.status_code != 200:
            raise ZettleError(f"GET {url} failed ({resp.status_code}).")
        return resp.json()

    def products(self) -> list[dict]:
        data = self._get(PRODUCTS_URL)
        return data.get("products", []) if isinstance(data, dict) else data

    def purchases(self, start_date_iso: str, limit: int = 100):
        """Yield purchases newest-first from start_date, following hash pagination."""
        last_hash = None
        while True:
            params = {"startDate": start_date_iso, "limit": limit, "descending": "true"}
            if last_hash:
                params["lastPurchaseHash"] = last_hash
            data = self._get(PURCHASES_URL, params)
            batch = data.get("purchases", [])
            yield from batch
            last_hash = data.get("lastPurchaseHash")
            if not batch or len(batch) < limit or not last_hash:
                return
