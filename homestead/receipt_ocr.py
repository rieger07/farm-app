"""Gemini Vision receipt OCR client with Structured Outputs (JSON Schema)."""

from __future__ import annotations

import base64
from datetime import date
import json
import logging
import math
import os
from typing import Any

import httpx

from homestead.db.herds import (
    normalize_herd_feed_category,
    normalize_herd_feed_unit,
)

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_OCR_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_OCR_THINKING_LEVEL = "LOW"
VALID_GEMINI_THINKING_LEVELS = ("MINIMAL", "LOW", "MEDIUM", "HIGH")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
OCR_TIMEOUT_SECONDS = 25.0
MAX_OCR_FILE_BYTES = 12 * 1024 * 1024

ALLOWED_OCR_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/gif",
    "application/pdf",
}

_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}

VALID_SPLIT_TYPES = (
    "general_farm_expense",
    "laying_flock_feed",
    "meat_batch_feed",
    "herd_feed",
)
VALID_SCHEDULE_F_CATEGORIES = (
    "supplies",
    "capital_equipment",
    "livestock",
    "processing",
    "market",
    "general",
)

RECEIPT_OCR_SYSTEM_PROMPT = (
    "You extract structured accounting data from farm receipts, invoices, PDFs, and mobile/web order screenshots.\n"
    "Rules:\n"
    "1. Parse paper receipts, PDFs, and mobile or web order screenshots accurately.\n"
    "2. Infer 'vendor' from logos, page headers, or navigation bars if not explicitly printed "
    "(e.g. 'TSC' or 'Neighbor\\'s Club' -> 'Tractor Supply').\n"
    "3. If a screenshot shows multiple orders in a list, extract ONLY the primary expanded order "
    "and ignore partially visible adjacent orders.\n"
    "4. Group receipt items into logical 'splits' matching the four supported types: "
    "'laying_flock_feed', 'meat_batch_feed', 'herd_feed', and 'general_farm_expense'. "
    "Use 'herd_feed' for ongoing breeding pigs/swine feed, alpaca pellets/grain, hay bales, or herd minerals/supplements. "
    "For poultry feed lines, extract 'bags' and 'total_weight_lb' when visible (e.g. '2x 50lb' -> bags: 2, total_weight_lb: 100), "
    "and set 'feed_type' to 'layer', 'scratch', or 'supplement' for laying_flock_feed or 'starter' / 'grower' for meat_batch_feed. "
    "For 'herd_feed', set 'feed_category' ('grain' | 'hay' | 'supplement'), 'unit' ('bags' | 'bales' | 'lbs'), "
    "'quantity' (count of bags/bales/lbs), 'total_weight_lb' when known, and 'herd_hint' (e.g. 'Breeding Pigs' or 'Alpacas').\n"
    "5. Sales Tax (NJ Farm Exemption): Most qualifying farm receipts have $0 tax — use exact line-item totals. "
    "If sales tax IS present (e.g. non-exempt items, mixed receipts, or missed exemption), return the tax amount "
    "in 'tax_charged' and apply tax only to taxed line items (often marked 'T' on paper receipts) or pro-rate it "
    "across taxable splits so the sum of splits[].amount equals receipt_total.\n"
    "6. Set 'schedule_f_category' to 'capital_equipment' for single durable equipment/machinery items costing "
    "$2,500 or more (e.g. mowers, tractors, trailers). Otherwise use 'supplies' (<$2,500 supplies/tools/bedding), "
    "'livestock' (chicks/poults/livestock), 'processing' (butcher/custom hire), 'market' (booth/selling fees), "
    "or 'general' (repairs/utilities/fuel/other). Use null for feed splits.\n"
    "7. Set 'mixed_use_warning' to true if any line items look like personal or non-farm purchases.\n"
    "8. Discounts vs. Refunds/Returns: Store promos, coupons, and loyalty rewards (e.g. Neighbor's Club Rewards) on a normal purchase "
    "receipt must be folded into the net cost of the purchased items — do NOT create separate negative split lines for normal purchase discounts. "
    "For actual item returns, refunds, or store credits (either a standalone return receipt or partial refunds shown on an order screenshot alongside purchased items), "
    "emit a split line with a negative 'amount' and prefix 'notes' with 'Refund: '. "
    "For returned feed bags, emit negative 'bags' and negative 'total_weight_lb' (or null for a price-adjustment credit where no physical bags were returned). "
    "For standalone return receipts, 'receipt_total' should be negative; on mixed purchase + return receipts, the sum of splits[].amount must reconcile with the net 'receipt_total'."
)

RECEIPT_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "date": {
            "type": "STRING",
            "nullable": True,
            "description": "Receipt date in YYYY-MM-DD format, or null if unreadable.",
        },
        "vendor": {"type": "STRING"},
        "receipt_total": {"type": "NUMBER"},
        "tax_charged": {"type": "NUMBER"},
        "mixed_use_warning": {"type": "BOOLEAN"},
        "splits": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "split_type": {
                        "type": "STRING",
                        "enum": list(VALID_SPLIT_TYPES),
                    },
                    "amount": {"type": "NUMBER"},
                    "notes": {"type": "STRING"},
                    "schedule_f_category": {
                        "type": "STRING",
                        "nullable": True,
                        "enum": list(VALID_SCHEDULE_F_CATEGORIES),
                    },
                    "feed_type": {"type": "STRING", "nullable": True},
                    "feed_category": {"type": "STRING", "nullable": True},
                    "unit": {"type": "STRING", "nullable": True},
                    "quantity": {"type": "NUMBER", "nullable": True},
                    "herd_hint": {"type": "STRING", "nullable": True},
                    "bags": {"type": "NUMBER", "nullable": True},
                    "total_weight_lb": {"type": "NUMBER", "nullable": True},
                },
                "required": ["split_type", "amount", "notes"],
            },
        },
    },
    "required": [
        "date",
        "vendor",
        "receipt_total",
        "tax_charged",
        "mixed_use_warning",
        "splits",
    ],
}


class ReceiptOCRError(Exception):
    """Typed error raised when Gemini Vision OCR fails or is unavailable."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")


def is_receipt_ocr_enabled() -> bool:
    return bool(get_gemini_api_key())


def get_gemini_thinking_level(override: str | None = None) -> str | None:
    raw = (
        override
        if override is not None
        else os.environ.get("GEMINI_OCR_THINKING_LEVEL", DEFAULT_GEMINI_OCR_THINKING_LEVEL)
    )
    clean = str(raw or "").strip().strip('"').strip("'").upper()
    if clean in ("OFF", "NONE", "DEFAULT"):
        return None
    if not clean:
        return DEFAULT_GEMINI_OCR_THINKING_LEVEL
    if clean in VALID_GEMINI_THINKING_LEVELS:
        return clean
    logger.warning(
        "Invalid GEMINI_OCR_THINKING_LEVEL=%r; falling back to %s",
        clean,
        DEFAULT_GEMINI_OCR_THINKING_LEVEL,
    )
    return DEFAULT_GEMINI_OCR_THINKING_LEVEL


def _extract_gemini_error_detail(resp: httpx.Response, secret_key: str) -> str:
    """Safely extract and sanitize error.message from an upstream Gemini response."""
    try:
        body = resp.json()
    except (ValueError, TypeError):
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if not isinstance(err, dict):
        return ""
    msg = err.get("message")
    if not isinstance(msg, str):
        return ""
    clean = " ".join(msg.split())
    if secret_key:
        clean = clean.replace(secret_key, "[redacted]")
    return clean[:180]


def detect_receipt_mime_type(
    content_type: str | None, filename: str | None = None
) -> str:
    """Resolve and validate the uploaded receipt MIME type (images or PDF)."""
    ct = str(content_type or "").split(";")[0].strip().lower()
    if ct == "image/jpg":
        ct = "image/jpeg"
    if ct in ALLOWED_OCR_MIME_TYPES:
        return ct
    fname = str(filename or "").strip().lower()
    for ext, mapped in _EXT_TO_MIME.items():
        if fname.endswith(ext):
            return mapped
    raise ValueError(
        "Unsupported file format. Upload a JPEG/PNG/WebP image or PDF receipt."
    )


def _safe_signed_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _safe_nonneg_float(val: Any) -> float | None:
    num = _safe_signed_float(val)
    if num is None or num < 0:
        return None
    return num


def _normalize_date(raw_date: Any) -> str | None:
    txt = str(raw_date or "").strip()[:10]
    if not txt:
        return None
    try:
        date.fromisoformat(txt)
        return txt
    except ValueError:
        return None


def normalize_parsed_receipt(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and coerce raw Gemini JSON into the guaranteed /api/receipts/parse shape."""
    if not isinstance(raw, dict):
        raise ValueError("OCR output was not a JSON object.")

    rdate = _normalize_date(raw.get("date"))
    vendor = str(raw.get("vendor") or "").strip()[:120]
    receipt_total = round(_safe_signed_float(raw.get("receipt_total")) or 0.0, 2)
    tax_charged = round(_safe_nonneg_float(raw.get("tax_charged")) or 0.0, 2)
    mixed_warn = bool(raw.get("mixed_use_warning"))

    clean_splits: list[dict[str, Any]] = []
    raw_splits = raw.get("splits")
    if isinstance(raw_splits, list):
        for item in raw_splits:
            if not isinstance(item, dict):
                continue
            amt = _safe_signed_float(item.get("amount"))
            if amt is None or round(amt, 2) == 0.0:
                continue
            amt_round = round(amt, 2)
            stype = str(item.get("split_type") or "general_farm_expense").strip().lower()
            if stype not in VALID_SPLIT_TYPES:
                stype = "general_farm_expense"

            notes = str(item.get("notes") or "").strip()[:240]
            raw_cat = item.get("schedule_f_category")
            cat: str | None
            if stype == "general_farm_expense":
                c_str = str(raw_cat or "").strip().lower()
                if c_str in VALID_SCHEDULE_F_CATEGORIES:
                    cat = c_str
                elif abs(amt_round) >= 2500.0 and any(
                    kw in notes.lower()
                    for kw in ("tractor", "mower", "trailer", "equipment", "utility vehicle")
                ):
                    cat = "capital_equipment"
                else:
                    cat = "supplies"
            else:
                cat = None

            feed_type_raw = item.get("feed_type")
            feed_type = str(feed_type_raw).strip().lower()[:40] if feed_type_raw else None
            bags = _safe_signed_float(item.get("bags"))
            lbs = _safe_signed_float(item.get("total_weight_lb"))
            qty = _safe_signed_float(item.get("quantity"))
            if amt_round < 0:
                bags = (-abs(bags) if bags else 0.0) if bags is not None else None
                lbs = (-abs(lbs) if lbs else 0.0) if lbs is not None else None
                qty = (-abs(qty) if qty else 0.0) if qty is not None else None
            else:
                bags = abs(bags) if bags is not None else None
                lbs = abs(lbs) if lbs is not None else None
                qty = abs(qty) if qty is not None else None

            if stype == "herd_feed":
                feed_category: str | None = normalize_herd_feed_category(item.get("feed_category"))
                unit: str | None = normalize_herd_feed_unit(item.get("unit"), feed_category)
                if qty is None and bags is not None and unit == "bags":
                    qty = bags
                raw_hint = str(item.get("herd_hint") or "").strip()[:80]
                herd_hint: str | None = raw_hint or None
            else:
                feed_category = None
                unit = None
                herd_hint = None

            clean_splits.append(
                {
                    "split_type": stype,
                    "amount": amt_round,
                    "notes": notes,
                    "schedule_f_category": cat,
                    "feed_type": feed_type,
                    "feed_category": feed_category,
                    "unit": unit,
                    "quantity": round(qty, 2) if qty is not None else None,
                    "herd_hint": herd_hint,
                    "bags": round(bags, 2) if bags is not None else None,
                    "total_weight_lb": round(lbs, 2) if lbs is not None else None,
                }
            )

    if not clean_splits and receipt_total != 0:
        fallback_notes = (
            f"Refund: {vendor}" if vendor else "Refund"
        ) if receipt_total < 0 else (vendor or "Scanned receipt")
        clean_splits.append(
            {
                "split_type": "general_farm_expense",
                "amount": receipt_total,
                "notes": fallback_notes,
                "schedule_f_category": "supplies",
                "feed_type": None,
                "bags": None,
                "total_weight_lb": None,
            }
        )

    return {
        "date": rdate,
        "vendor": vendor,
        "receipt_total": receipt_total,
        "tax_charged": tax_charged,
        "mixed_use_warning": mixed_warn,
        "splits": clean_splits,
    }


def _strip_json_fences(text: str) -> str:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            clean = "\n".join(lines[1:-1]).strip()
    return clean


async def parse_receipt_with_gemini(
    file_bytes: bytes,
    mime_type: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    thinking_level: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Call Gemini Vision API with Structured Outputs and return normalized receipt JSON."""
    key = (api_key if api_key is not None else get_gemini_api_key()).strip()
    if not key:
        raise ReceiptOCRError("GEMINI_API_KEY is not configured.", status_code=503)
    if not file_bytes:
        raise ReceiptOCRError("Uploaded receipt file is empty.", status_code=400)
    if len(file_bytes) > MAX_OCR_FILE_BYTES:
        raise ReceiptOCRError("Receipt file exceeds the 12 MB size limit.", status_code=413)

    resolved_mime = detect_receipt_mime_type(mime_type)
    model_name = (
        (model or os.environ.get("GEMINI_OCR_MODEL", DEFAULT_GEMINI_OCR_MODEL))
        .strip()
        .strip('"')
        .strip("'")
        or DEFAULT_GEMINI_OCR_MODEL
    )
    url = f"{GEMINI_API_BASE}/{model_name}:generateContent"

    generation_config: dict[str, Any] = {
        "temperature": 0.1,
        "responseMimeType": "application/json",
        "responseSchema": RECEIPT_RESPONSE_SCHEMA,
    }
    resolved_thinking = get_gemini_thinking_level(thinking_level)
    if resolved_thinking:
        generation_config["thinkingConfig"] = {"thinkingLevel": resolved_thinking}

    payload = {
        "systemInstruction": {"parts": [{"text": RECEIPT_OCR_SYSTEM_PROMPT}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": "Extract the primary receipt/order date, vendor, totals, tax, and categorized farm expense splits."
                    },
                    {
                        "inlineData": {
                            "mimeType": resolved_mime,
                            "data": base64.b64encode(file_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": generation_config,
    }

    owns_client = client is None
    http_client = (
        httpx.AsyncClient(timeout=OCR_TIMEOUT_SECONDS)
        if owns_client
        else client
    )
    try:
        resp = await http_client.post(
            url,
            headers={
                "x-goog-api-key": key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        logger.warning(
            "Gemini receipt OCR timed out after %.1fs (model=%s)",
            OCR_TIMEOUT_SECONDS,
            model_name,
        )
        raise ReceiptOCRError(
            "Receipt scan timed out after 25s — please enter details manually.",
            status_code=422,
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "Gemini receipt OCR network error (model=%s): %s",
            model_name,
            type(exc).__name__,
        )
        raise ReceiptOCRError(
            "Could not reach Gemini Vision API — please enter details manually.",
            status_code=422,
        ) from exc
    finally:
        if owns_client:
            await http_client.aclose()

    if resp.status_code >= 400:
        detail = _extract_gemini_error_detail(resp, key)
        logger.warning(
            "Gemini receipt OCR HTTP %s (model=%s): %s",
            resp.status_code,
            model_name,
            detail or "<no detail>",
        )
        out_status = 429 if resp.status_code == 429 else 422
        if detail:
            msg = f"Receipt scan failed (Gemini HTTP {resp.status_code}: {detail}) — please enter details manually."
        else:
            msg = f"Receipt scan failed (Gemini HTTP {resp.status_code}) — please enter details manually."
        raise ReceiptOCRError(msg, status_code=out_status)

    try:
        body = resp.json()
        parts = body["candidates"][0]["content"]["parts"]
        text = next(
            (
                str(p.get("text") or "")
                for p in parts
                if isinstance(p, dict) and p.get("text") and not p.get("thought")
            ),
            "",
        )
        parsed = json.loads(_strip_json_fences(text))
        return normalize_parsed_receipt(parsed)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "Gemini receipt OCR parse error (model=%s): %s",
            model_name,
            type(exc).__name__,
        )
        raise ReceiptOCRError(
            "Could not parse receipt OCR response — please enter details manually.",
            status_code=422,
        ) from exc
