# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models


class PosPaymentMethod(models.Model):
    _inherit = "pos.payment.method"

    def fio_check_qr_payment(self, amount, pos_reference, currency_id):
        """Volá pokladna, dokud je otevřený QR kód platby.

        Stáhne nové pohyby z Fio (nejvýš jednou za 30 s) a hledá příchozí pohyb ve stejné
        částce s číslem objednávky ve zprávě. Když číslo chybí, vezme jediný dnešní nebo
        včerejší nespárovaný pohyb ve stejné částce (platba právě probíhá, plátce zprávu
        neposlal). Nalezený pohyb si rezervuje pro objednávku; spáruje se po jejím uložení.
        """
        self.ensure_one()
        bank = self.sudo().journal_id.bank_account_id
        if not bank or not bank.fio_api_token:
            return {"enabled": False}
        bank.fio_sync_recent()
        Transaction = self.env["fio.bank.transaction"].sudo()
        candidates = Transaction.search([
            ("bank_account_id", "=", bank.id),
            ("state", "=", "new"),
            ("amount", ">=", amount - 0.005),
            ("amount", "<=", amount + 0.005),
        ], order="date desc, id desc", limit=50)
        match, match_type = None, None
        for tx in candidates:
            if tx.reserved_reference and tx.reserved_reference != pos_reference:
                continue
            if tx._text_matches_reference(pos_reference):
                match, match_type = tx, "reference"
                break
        if not match:
            since = fields.Date.context_today(self) - timedelta(days=1)
            recent = candidates.filtered(
                lambda t: t.date and t.date >= since
                and (not t.reserved_reference or t.reserved_reference == pos_reference)
            )
            if len(recent) == 1:
                match, match_type = recent[0], "amount"
        if match:
            match.reserved_reference = pos_reference
            return {
                "enabled": True,
                "confirmed": True,
                "transaction_id": match.id,
                "match_type": match_type,
                "counter_name": match.counter_name or "",
                "date": fields.Date.to_string(match.date) if match.date else "",
            }
        return {
            "enabled": True,
            "confirmed": False,
            "next_check_in": bank._fio_seconds_until_allowed(),
        }
