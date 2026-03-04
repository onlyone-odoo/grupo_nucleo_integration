# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

"""
HTTP client for Grupo Núcleo API.
Handles JWT auth (15 min validity) and endpoints: GetCatalog, CheckoutConfirm, NewSelfSaleOrder.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional


class GrupNucleoAPIError(Exception):
    """Raised when an API call fails."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Optional[str] = None):
        self.status_code = status_code
        self.body = body
        super().__init__(message)


class GrupNucleoAPI:
    """Client for Grupo Núcleo API (https://api.gruponucleosa.com)."""

    DEFAULT_BASE_URL = "https://api.gruponucleosa.com"
    TOKEN_BUFFER_SECONDS = 60  # Refresh token before expiry (15 min = 900 s)

    def __init__(
        self,
        base_url: str,
        api_id: int,
        username: str,
        password: str,
        token: Optional[str] = None,
        token_expiry_ts: Optional[float] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_id = api_id
        self.username = username
        self.password = password
        self._token = token
        self._token_expiry_ts = token_expiry_ts or 0.0

    # User-Agent accepted by Cloudflare (avoids "Access denied" when default Python-urllib is blocked)
    USER_AGENT = "Mozilla/5.0 (compatible; Odoo/17; GrupoNucleo-Integration/1.0)"

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict | list] = None,
        use_auth: bool = True,
    ) -> tuple[int, Any]:
        """Perform HTTP request. Returns (status_code, response_data). response_data is parsed JSON or None."""
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
        }
        if use_auth:
            token = self._get_token()
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
                try:
                    return status, json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    return status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8") if e.fp else ""
            code = e.code
            if code == 403:
                msg = (
                    "Forbidden (403): La API rechazó el acceso. Verifique con Grupo Núcleo que su cuenta tenga "
                    "permiso para API y que no haya restricciones (IP, usuario activo). "
                )
            else:
                msg = str(e.reason) or f"HTTP {code}"
            if raw:
                msg += f" Respuesta: {raw[:400]}"
            raise GrupNucleoAPIError(msg, status_code=code, body=raw) from e

    def _get_token(self) -> str:
        """Return valid JWT; refresh if expired or missing."""
        if self._token and time.time() < (self._token_expiry_ts - self.TOKEN_BUFFER_SECONDS):
            return self._token
        payload = {
            "id": self.api_id,
            "username": self.username,
            "password": self.password,
        }
        status, data = self._request(
            "POST",
            "/Authentication/Login",
            body=payload,
            use_auth=False,
        )
        body_str = data if isinstance(data, str) else (json.dumps(data) if data is not None else "")
        if status != 200:
            if status == 403:
                msg = (
                    "Forbidden (403): La API denegó el acceso. Confirme con Grupo Núcleo que su usuario tiene "
                    "acceso a la API (credenciales eCommerce) y que no hay restricciones por IP o cuenta."
                )
            else:
                msg = f"Login falló (HTTP {status}). Revise API ID, usuario y contraseña."
            if body_str:
                msg += f" Respuesta API: {body_str[:400]}"
            raise GrupNucleoAPIError(msg, status_code=status, body=body_str)
        # API may return: plain JWT string, or JSON with access_token/token (doc shows raw JWT)
        if isinstance(data, str):
            token = data.strip().strip('"')
        elif isinstance(data, dict):
            token = (
                data.get("access_token")
                or data.get("accessToken")
                or data.get("token")
                or data.get("Token")
                or ""
            )
            if isinstance(token, str):
                token = token.strip()
            else:
                token = ""
        else:
            token = str(data).strip() if data else ""
        if not token or len(token) < 50:
            msg = "Login did not return a valid token. Check credentials and API response."
            if body_str:
                msg += f" API response: {body_str[:500]}"
            raise GrupNucleoAPIError(msg, status_code=status, body=body_str)
        self._token = token
        # JWT valid 15 min; we don't decode JWT here, assume 15 min
        self._token_expiry_ts = time.time() + (15 * 60)
        return self._token

    def get_catalog(self) -> Any:
        """GET /API_V1/GetCatalog. Returns catalog data (list of products with price, tax, stock, images)."""
        status, data = self._request("GET", "/API_V1/GetCatalog")
        if status != 200:
            raise GrupNucleoAPIError(
                f"GetCatalog failed with status {status}",
                status_code=status,
                body=str(data),
            )
        return data

    def checkout_confirm(self, item_ids: list[int]) -> Any:
        """POST /API_V1/CheckoutConfirm. Verify price and stock for up to 15 items. item_ids = list of item_id."""
        if len(item_ids) > 15:
            raise GrupNucleoAPIError("CheckoutConfirm accepts at most 15 items per request")
        status, data = self._request("POST", "/API_V1/CheckoutConfirm", body=item_ids)
        if status != 200:
            raise GrupNucleoAPIError(
                f"CheckoutConfirm failed with status {status}",
                status_code=status,
                body=str(data),
            )
        return data

    def new_self_sale_order(self, nota: str, items: list[dict]) -> Any:
        """
        POST /API_V1_SSO/NewSelfSaleOrder.
        items: list of {"item_id": int, "item_qty": int}
        nota: max 350 characters (API truncates).
        """
        nota_trimmed = (nota or "")[:350]
        payload = {"nota": nota_trimmed, "items": items}
        status, data = self._request("POST", "/API_V1_SSO/NewSelfSaleOrder", body=payload)
        if status != 200:
            raise GrupNucleoAPIError(
                f"NewSelfSaleOrder failed with status {status}",
                status_code=status,
                body=str(data),
            )
        return data
