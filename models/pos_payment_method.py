# -*- coding: utf-8 -*-
from odoo import fields, models

from .fio_bank_transaction import AMOUNT_TOLERANCE


class PosPaymentMethod(models.Model):
    _inherit = "pos.payment.method"

    def fio_check_qr_payment(self, amount, pos_reference, currency_id):
        """Volá pokladna, dokud je otevřený QR kód platby.

        Stáhne nové pohyby z Fio dalším tokenem v pořadí (interval 30 s / počet tokenů) a hledá
        příchozí pohyb, který platbu pokrývá (stejná částka nebo vyšší o dýško, nikdy nižší)
        a má číslo objednávky ve zprávě. Nalezený pohyb si rezervuje pro objednávku; spáruje se
        po jejím uložení.

        Jen podle částky se nepotvrzuje nikdy: do 20.0.1.2.1 se bral i jediný dnešní nebo
        včerejší pohyb bez čísla objednávky v částce platby až platby + 50 % a pokladna tak
        potvrdila nezaplacenou objednávku (494 Kč proti převodu 600 Kč z vlastního účtu
        z předchozího dne). Pohyb bez čísla objednávky se páruje jen ručně.
        """
        self.ensure_one()
        bank = self.sudo().journal_id.bank_account_id
        if not bank or not bank.fio_enabled:
            return {"enabled": False}
        synced = bank.fio_sync_recent()
        Transaction = self.env["fio.bank.transaction"].sudo()
        candidates = Transaction.search([
            ("bank_account_id", "=", bank.id),
            ("state", "=", "new"),
            ("amount", ">=", amount - AMOUNT_TOLERANCE),
        ], order="date desc, id desc", limit=100)
        candidates = candidates.filtered(
            lambda t: not t.reserved_reference or t.reserved_reference == pos_reference
        )
        match = next((tx for tx in candidates if tx._text_matches_reference(pos_reference)), None)
        if match:
            match.reserved_reference = pos_reference
            return {
                "enabled": True,
                "confirmed": True,
                "transaction_id": match.id,
                "match_type": "reference",
                "counter_name": match.counter_name or "",
                "date": fields.Date.to_string(match.date) if match.date else "",
                "amount": match.amount,
                "tip": match._tip_for(amount),
            }
        return {
            "enabled": True,
            "confirmed": False,
            # pro odpočet v pokladně: právě teď staženo z banky? za kolik sekund to jde znovu
            # a jaký je skutečný interval mezi dotazy (30 s / počet tokenů)
            "synced": bool(synced),
            "next_check_in": bank._fio_seconds_until_allowed(),
            "interval": bank._fio_interval_seconds(),
            "token_count": len(bank._fio_tokens()),
        }
