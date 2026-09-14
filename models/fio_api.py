# -*- coding: utf-8 -*-
"""Klient Fio REST API (https://www.fio.cz/docs/cz/API_Bankovnictvi.pdf).

Čisté funkce bez ORM, aby šly snadno testovat a nahradit. Token je 64 znaků,
vydává se v internetovém bankovnictví per účet; pro párování stačí práva jen ke čtení.
Fio dovolí **jeden dotaz za 30 sekund** na token, při rychlejším volání vrací HTTP 409.
"""
import logging
import re
import unicodedata
from datetime import date

import requests

_logger = logging.getLogger(__name__)

FIO_BASE_URL = "https://fioapi.fio.cz/v1/rest"
RATE_LIMIT_SECONDS = 30
REQUEST_TIMEOUT = 40

# Sloupce výpisu podle dokumentace Fio (column<N> -> náš klíč)
COLUMNS = {
    "column22": "fio_id",               # ID pohybu
    "column0": "date",                  # Datum ("2026-09-09+0200")
    "column1": "amount",                # Objem
    "column14": "currency",             # Měna
    "column2": "counter_account",       # Protiúčet
    "column3": "counter_bank_code",     # Kód banky protiúčtu
    "column10": "counter_name",         # Název protiúčtu
    "column4": "constant_symbol",       # KS
    "column5": "variable_symbol",       # VS
    "column6": "specific_symbol",       # SS
    "column7": "user_identification",   # Uživatelská identifikace
    "column16": "message",              # Zpráva pro příjemce
    "column8": "transaction_type",      # Typ
    "column9": "executed_by",           # Provedl
    "column18": "specification",        # Upřesnění
    "column25": "comment",              # Komentář
    "column26": "bic",                  # BIC
    "column17": "instruction_id",       # ID pokynu
}


class FioApiError(Exception):
    """Chyba komunikace s Fio (neplatný token, výpadek, neočekávaná odpověď)."""


class FioRateLimitError(FioApiError):
    """Fio vrátilo 409: víc než jeden dotaz za 30 sekund."""


def _get(url):
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise FioApiError(f"Fio API nedostupné: {exc}") from exc
    if response.status_code == 409:
        raise FioRateLimitError("Fio API: víc než jeden dotaz za 30 sekund (HTTP 409)")
    if response.status_code != 200:
        detail = " ".join((response.text or "").split())[:300]
        hint = {
            404: "špatně zaslaný dotaz (parametry URL)",
            413: "příliš mnoho pohybů (limit 50 000), zkrať období",
            422: "data starší 90 dní bez silné autorizace: v internetbankingu Fio Nastavení › API klikni u tokenu "
                 "na ikonu zámku a autorizuj přístup k historii (platí 10 minut), pak akci zopakuj",
            500: "neexistující nebo neaktivní token (nový token funguje 5 minut po autorizaci, platnost nejdéle 180 dní)",
        }.get(response.status_code, "neočekávaná odpověď")
        raise FioApiError(f"Fio API: HTTP {response.status_code} – {hint}" + (f" ({detail})" if detail else ""))
    return response


def _safe_url(url, token):
    return url.replace(token, "<token>")


def fetch_periods(token, date_from, date_to):
    """Pohyby v období (včetně obou dnů). Vrací seznam slovníků podle COLUMNS."""
    url = f"{FIO_BASE_URL}/periods/{token}/{date_from:%Y-%m-%d}/{date_to:%Y-%m-%d}/transactions.json"
    _logger.info("Fio: periods %s", _safe_url(url, token))
    return parse_transactions(_get(url).json())


def fetch_last(token):
    """Pohyby od posledního stažení (Fio si posouvá zarážku samo)."""
    url = f"{FIO_BASE_URL}/last/{token}/transactions.json"
    _logger.info("Fio: last %s", _safe_url(url, token))
    return parse_transactions(_get(url).json())


def set_last_id(token, fio_id):
    """Nastaví zarážku pro `last` za pohyb s daným ID."""
    url = f"{FIO_BASE_URL}/set-last-id/{token}/{fio_id}/"
    _logger.info("Fio: set-last-id %s", _safe_url(url, token))
    _get(url)


def set_last_date(token, day):
    """Nastaví zarážku pro `last` na daný den (pohyby od něj včetně)."""
    url = f"{FIO_BASE_URL}/set-last-date/{token}/{day:%Y-%m-%d}/"
    _logger.info("Fio: set-last-date %s", _safe_url(url, token))
    _get(url)


def parse_transactions(payload):
    """Převede JSON výpis Fio na seznam plochých slovníků.

    Hodnoty sloupců jsou v JSON jako {"value": ..., "name": ..., "id": N} nebo null.
    """
    try:
        transactions = payload["accountStatement"]["transactionList"]["transaction"]
    except (KeyError, TypeError) as exc:
        raise FioApiError("Fio API: neočekávaný tvar odpovědi") from exc
    result = []
    for raw in transactions or []:
        item = {}
        for column, key in COLUMNS.items():
            cell = raw.get(column)
            item[key] = cell.get("value") if isinstance(cell, dict) else None
        item["fio_id"] = str(item["fio_id"]) if item["fio_id"] is not None else None
        item["date"] = parse_fio_date(item["date"])
        item["amount"] = float(item["amount"] or 0.0)
        for key in ("variable_symbol", "constant_symbol", "specific_symbol", "counter_account", "counter_bank_code"):
            if item[key] is not None:
                item[key] = str(item[key]).strip()
        item["raw"] = raw
        if item["fio_id"]:
            result.append(item)
    return result


def parse_fio_date(value):
    """Fio posílá datum jako '2026-09-09+0200'; vrací date nebo None."""
    if not value:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    if not match:
        return None
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def normalize_text(value):
    """Text pro porovnání: bez diakritiky, malá písmena, bez mezer a bílých znaků."""
    folded = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", "", folded).lower()
