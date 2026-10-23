"""HubSpot client for the onboarding pipeline: READ-ONLY, plus one narrow writer.

Bearer token from env HUBSPOT_TOKEN (the private app "Famly_Server_Key").

HARD RULES, each pinned by tests:

  * `HubSpotClient` IS READ ONLY. The only HTTP method it can send is GET
    (`_get`). There is no POST/PUT/PATCH/DELETE code path, so nothing on it can
    write to HubSpot. Contacts and quotes are read one at a time with GET rather
    than via the POST batch-read endpoint for exactly this reason.
  * `HubSpotDealWriter` (below) is the ONE writer: it can PATCH a deal, and only
    the sync properties and `end_date` in WRITABLE_DEAL_PROPERTIES (phase 2). Nothing else
    is writable through it: not another property, not a contact, not a quote.
  * EXPLICIT ALLOW-LISTS. Every object read names the properties it wants
    (`DEAL_PROPERTIES`, `CONTACT_PROPERTIES`, `QUOTE_PROPERTIES`). Never "all
    properties": a contact also holds national insurance numbers, dates of
    birth and photos that must never be requested.
  * NEVER LOGS PROPERTY VALUES. A log line carries the method, path and status
    only; an error carries the status and path, never a response body.
  * RETRIES 429 and 5xx with backoff (honouring Retry-After), then raises.

Values read here (names, emails, dates of birth) are held in memory by the
caller for a single run only: never stored, logged or printed (CLAUDE.md).
"""

import logging
import os
import time
from typing import Any, Callable

import requests

from core.config import ConfigError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"
DEFAULT_TIMEOUT = 30
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0

# The deal properties the dry run reads. `dealname` is requested but must never
# be printed or logged (it can be a child's name).
DEAL_PROPERTIES = (
    "dealname",
    "dealstage",
    "pipeline",
    "child_urn",
    "child_date_of_birth",
    "actual_start_date_1",
    "end_date",
    "location_of_interest",
    "deposit_amount",
    "famly_childid",
)
# Child name properties, used ONLY for the in-memory possible-duplicate check
# before a child would be created; never printed or logged. THE INTERNAL NAMES
# are `child_first_name` and `child_last_name` (design doc, section 16) -- override with
# HUBSPOT_CHILD_NAME_PROPERTIES (comma-separated). HubSpot ignores a property
# that does not exist, and with none returned the duplicate check treats every
# same-site, same-birth-date child as a possible duplicate (the safe side).
CHILD_NAME_PROPERTIES = tuple(
    p.strip()
    for p in os.environ.get(
        "HUBSPOT_CHILD_NAME_PROPERTIES",
        "child_first_name,child_last_name",
    ).split(",")
    if p.strip()
)
DEAL_PROPERTIES = DEAL_PROPERTIES + tuple(
    p for p in CHILD_NAME_PROPERTIES if p not in DEAL_PROPERTIES
)
# The contact's email (to match it) plus the fields a Famly contact would be
# built from. Values are read into memory only to derive booleans and codes;
# the plan never carries them. The WhatsApp property name is HubSpot's standard
# one ("WhatsApp phone number"); override with HUBSPOT_WHATSAPP_PROPERTY.
WHATSAPP_PROPERTY = os.environ.get("HUBSPOT_WHATSAPP_PROPERTY", "hs_whatsapp_phone_number")
CONTACT_PROPERTIES = (
    "email",
    "firstname",
    "lastname",
    "mobilephone",
    "phone",
    WHATSAPP_PROPERTY,
    "address",
    "city",
    "zip",
    "state",
    "country",
)
QUOTE_PROPERTIES = ("hs_status", "hs_quote_esign_status", "hs_manually_signed")

# Association labels (verified by Drew in HubSpot's API details, 2026-10-15).
# Matched by LABEL and INTERNAL NAME first; the type id is only a cross-check
# and is reported when it differs. Each can be overridden from the environment.
BILL_PAYER_LABEL = os.environ.get("HUBSPOT_BILL_PAYER_LABEL", "Bill Payer")
BILL_PAYER_INTERNAL_NAME = os.environ.get(
    "HUBSPOT_BILL_PAYER_INTERNAL_NAME", "bill_payer_childcare_account"
)
BILL_PAYER_TYPE_ID = int(os.environ.get("HUBSPOT_BILL_PAYER_TYPE_ID", "91"))
SALES_LABEL = os.environ.get("HUBSPOT_SALES_LABEL", "Sales")
SALES_INTERNAL_NAME = os.environ.get("HUBSPOT_SALES_INTERNAL_NAME", "sales_operations")
SALES_OPERATIONS_TYPE_ID = int(os.environ.get("HUBSPOT_SALES_OPERATIONS_TYPE_ID", "138"))


class HubSpotError(RuntimeError):
    """A HubSpot call failed. Carries the status and path, never a body."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HubSpotConfigError(ConfigError):
    """HUBSPOT_TOKEN is missing."""


class HubSpotClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = BASE_URL,
    ):
        token = (token if token is not None else os.environ.get("HUBSPOT_TOKEN", "")).strip()
        if not token:
            raise HubSpotConfigError("HUBSPOT_TOKEN is not set")
        self._token = token
        self._session = session or requests.Session()
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------ #
    # The one and only transport. GET, with retry.
    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._call("GET", path, params)

    def _call(
        self, method: str, path: str, params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """The one transport: send, retry 429/5xx with backoff, return JSON.

        Only `_get`, `search_deals` (a read) and the writer's PATCH call this."""
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if method == "GET":
                    response = self._session.get(
                        url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT
                    )
                else:
                    response = getattr(self._session, method.lower())(
                        url, headers=headers, json=json_body, timeout=DEFAULT_TIMEOUT
                    )
            except requests.RequestException as exc:
                # Type only: the message can echo the URL.
                if attempt == MAX_ATTEMPTS:
                    raise HubSpotError(f"{method} {path} failed: {type(exc).__name__}") from None
                self._sleep(self._backoff(attempt, None))
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise HubSpotError(f"{method} {path} failed with HTTP {status}", status)
                logger.info("hubspot: %s %s -> %s, retrying", method, path, status)
                self._sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                continue

            if status != 200:
                raise HubSpotError(f"{method} {path} failed with HTTP {status}", status)

            try:
                body = response.json()
            except ValueError:
                raise HubSpotError(f"{method} {path} returned a non-JSON body", status) from None
            return body if isinstance(body, dict) else {}

        raise HubSpotError(f"{method} {path} failed")  # unreachable; loop always returns/raises

    @staticmethod
    def _backoff(attempt: int, retry_after: Any) -> float:
        try:
            if retry_after is not None:
                return min(float(retry_after), BACKOFF_CAP_SECONDS)
        except (TypeError, ValueError):
            pass
        return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)

    # ------------------------------------------------------------------ #
    # Objects (allow-listed properties only)
    # ------------------------------------------------------------------ #
    def get_deal(self, deal_id: str) -> dict:
        """The deal's allow-listed properties, as a flat dict. Raises HubSpotError
        (status 404) if there is no such deal."""
        body = self._get(
            f"/crm/v3/objects/deals/{deal_id}",
            {"properties": ",".join(DEAL_PROPERTIES)},
        )
        return dict(body.get("properties") or {})

    def search_deals(self, filters: list[dict], properties: list[str], limit: int = 100) -> list[dict]:
        """READ-ONLY deal search (HubSpot's search endpoint is a POST, but it only reads).

        `filters` is ONE filter group (ANDed): [{"propertyName", "operator", "value"}].
        Returns every match's `{"id", **properties}` over all pages, requesting only the
        named `properties`. Used for the child URN checks; it is the one POST this class
        sends and the endpoint cannot write."""
        found: list[dict] = []
        after = None
        for _ in range(500):                                  # a runaway cursor must end
            body = {"filterGroups": [{"filters": filters}], "properties": list(properties),
                    "limit": limit}
            if after:
                body["after"] = after
            page = self._call("POST", "/crm/v3/objects/deals/search", json_body=body)
            for row in page.get("results") or []:
                if isinstance(row, dict):
                    found.append({"id": str(row.get("id")), **(row.get("properties") or {})})
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return found
        raise HubSpotError("deal search did not end")

    def get_contact(self, contact_id: str) -> dict:
        body = self._get(
            f"/crm/v3/objects/contacts/{contact_id}",
            {"properties": ",".join(CONTACT_PROPERTIES)},
        )
        return dict(body.get("properties") or {})

    def get_quote(self, quote_id: str) -> dict:
        body = self._get(
            f"/crm/v3/objects/quotes/{quote_id}",
            {"properties": ",".join(QUOTE_PROPERTIES)},
        )
        return dict(body.get("properties") or {})

    def stage_names(self) -> dict[str, tuple[str, str]]:
        """Deal stage id -> (pipeline label, stage label), for every pipeline."""
        body = self._get("/crm/v3/pipelines/deals")
        names: dict[str, tuple[str, str]] = {}
        for pipeline in body.get("results") or []:
            for stage in pipeline.get("stages") or []:
                names[str(stage.get("id"))] = (
                    str(pipeline.get("label") or ""),
                    str(stage.get("label") or ""),
                )
        return names

    # ------------------------------------------------------------------ #
    # Associations (v4), with labels
    # ------------------------------------------------------------------ #
    def association_labels(self, from_type: str, to_type: str) -> list[dict]:
        """The association definitions from `from_type` to `to_type`:
        [{"typeId", "label", "category", "name"?}, ...]."""
        body = self._get(f"/crm/v4/associations/{from_type}/{to_type}/labels")
        return [dict(item) for item in body.get("results") or [] if isinstance(item, dict)]

    def associations(self, from_type: str, from_id: str, to_type: str) -> list[dict]:
        """Objects of `to_type` associated with the object, each as
        {"id": str, "types": [{"typeId", "label", "category"}, ...]}. Follows
        paging."""
        out: list[dict] = []
        after: str | None = None
        while True:
            params = {"limit": 500}
            if after:
                params["after"] = after
            body = self._get(
                f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}", params
            )
            for item in body.get("results") or []:
                if not isinstance(item, dict) or item.get("toObjectId") is None:
                    continue
                out.append(
                    {
                        "id": str(item["toObjectId"]),
                        "types": [
                            dict(t)
                            for t in item.get("associationTypes") or []
                            if isinstance(t, dict)
                        ],
                    }
                )
            after = ((body.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return out


# --------------------------------------------------------------------------- #
# Phase 2: the one writer
# --------------------------------------------------------------------------- #
# The only deal properties anything may write, and only through the writer below.
FAMLY_CHILDID_PROPERTY = "famly_childid"
SYNC_STATUS_PROPERTY = "famly_sync_status"
SYNC_NOTE_PROPERTY = "famly_sync_note"
SYNC_GAPS_PROPERTY = "famly_sync_gaps"
# The deal's own end date: written ONLY when it was empty and the pipeline
# DEFAULTED one (after the Famly write succeeded); never overwritten.
END_DATE_PROPERTY = "end_date"
# The child URN the pipeline ALLOCATED (ONBOARD_URN_GENERATION=on), written ONLY to a deal
# that has none, before the Famly child is created; never overwritten.
CHILD_URN_PROPERTY = "child_urn"
WRITABLE_DEAL_PROPERTIES = frozenset(
    {
        CHILD_URN_PROPERTY,
        FAMLY_CHILDID_PROPERTY,
        SYNC_STATUS_PROPERTY,
        SYNC_NOTE_PROPERTY,
        SYNC_GAPS_PROPERTY,
        END_DATE_PROPERTY,
    }
)


class HubSpotDealWriter(HubSpotClient):
    """A HubSpotClient that can also write the sync properties (and a defaulted
    `end_date`) on a deal.

    `write_deal_properties` refuses any other property name before sending
    anything, and the values are codes and a Famly child id (no personal data).
    PATCH of the same values is idempotent, so a retry cannot change the outcome.
    """

    def deal_property_definitions(self) -> dict[str, dict]:
        """Every deal property: name -> {"type", "fieldType", "options"} (GET)."""
        body = self._get("/crm/v3/properties/deals")
        out: dict[str, dict] = {}
        for item in body.get("results") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            out[str(item["name"])] = {
                "type": item.get("type"),
                "fieldType": item.get("fieldType"),
                "options": [
                    str(o.get("value"))
                    for o in item.get("options") or []
                    if isinstance(o, dict) and o.get("value") is not None
                ],
            }
        return out

    def write_deal_properties(self, deal_id: str, properties: dict) -> None:
        """PATCH the deal's sync properties.

        Raises:
            ValueError: a property outside WRITABLE_DEAL_PROPERTIES (nothing sent).
            HubSpotError: the request failed.
        """
        extra = set(properties) - WRITABLE_DEAL_PROPERTIES
        if extra or not properties:
            raise ValueError(f"refusing to write deal properties: {sorted(extra) or 'none given'}")
        self._call(
            "PATCH",
            f"/crm/v3/objects/deals/{deal_id}",
            json_body={"properties": {k: str(v) for k, v in properties.items()}},
        )
