# -*- coding: utf-8 -*-
from datetime import datetime, time, timedelta

import pytz

from odoo import api, fields, models

from .fio_api import RATE_LIMIT_SECONDS

TIPS_ACTION_XMLID = "fio_qr_payment_validation.action_fio_tips"

FIO_STATES = [
    ("none", "Bez ověření"),
    ("pending", "Čeká na banku"),
    ("confirmed", "Potvrzeno bankou"),
    ("manual", "Potvrzeno ručně"),
    ("ignored", "Ignorováno"),
]

# Pole, která pokladna nemá načítat: Odoo 19 posílá pokladně u plateb všechna pole a při
# uložení objednávky je vrací (nová platba by přišla s fio_state=False a spadla na NOT NULL;
# stav ověření patří jen serveru).
FIO_SERVER_ONLY_FIELDS = (
    "fio_state", "fio_transaction_id", "fio_confirmed_at", "fio_match_type", "fio_tip_amount", "pos_reference",
)


class PosPayment(models.Model):
    _inherit = "pos.payment"

    fio_state = fields.Selection(
        FIO_STATES, string="Ověření Fio", default="none", required=True, index=True, copy=False,
        help="Čeká na banku: QR platba, kterou banka ještě nepotvrdila. Potvrzeno bankou: pohyb "
        "na účtu Fio dorazil a byl spárován. Potvrzeno ručně: zkontrolováno obsluhou bez pohybu.",
    )
    fio_transaction_id = fields.Many2one("fio.bank.transaction", string="Pohyb Fio", copy=False, index=True)
    fio_confirmed_at = fields.Datetime(string="Potvrzeno", copy=False)
    fio_match_type = fields.Selection(related="fio_transaction_id.match_type", string="Způsob spárování")
    fio_tip_amount = fields.Monetary(related="fio_transaction_id.tip_amount", string="Dýško", currency_field="currency_id")
    pos_reference = fields.Char(related="pos_order_id.pos_reference", string="Číslo objednávky")

    def _is_fio_qr_payment(self):
        self.ensure_one()
        method = self.payment_method_id
        return method.payment_method_type == "qr_code" or bool(method.qr_code_method)

    @api.model
    def _load_pos_data_fields(self, config):
        """Pokladna dostane všechna pole platby kromě těch, která řeší jen server (viz FIO_SERVER_ONLY_FIELDS)."""
        fields_list = super()._load_pos_data_fields(config) or list(self.fields_get(attributes=[]))
        return [name for name in fields_list if name not in FIO_SERVER_ONLY_FIELDS]

    @api.model
    def _fio_sanitize_vals(self, vals):
        """Prázdný stav ověření z pokladny (objednávky uložené před aktualizací) nechá na výchozí hodnotě."""
        if "fio_state" in vals and not vals["fio_state"]:
            vals.pop("fio_state")
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._fio_sanitize_vals(vals)
        payments = super().create(vals_list)
        for payment in payments:
            if payment.fio_state == "none" and payment._is_fio_qr_payment() and not payment.is_change:
                payment.fio_state = "pending"
                payment._fio_try_match()
        return payments

    def write(self, vals):
        return super().write(self._fio_sanitize_vals(dict(vals)))

    def _fio_try_match(self):
        """Po uložení objednávky zkusí najít už stažený pohyb (rozpoznaný při otevřeném QR nebo cronem).

        Pohyb musí platbu pokrývat: stejná částka nebo vyšší (dýško), nikdy nižší.
        """
        self.ensure_one()
        Transaction = self.env["fio.bank.transaction"].sudo()
        bank = self.payment_method_id.journal_id.bank_account_id
        domain = [
            ("state", "=", "new"),
            ("amount", ">=", self.amount - 0.005),
        ]
        if bank:
            domain.append(("bank_account_id", "=", bank.id))
        candidates = Transaction.search(domain, order="date desc, id desc", limit=100)
        pos_reference = self.pos_order_id.pos_reference
        reserved = candidates.filtered(lambda t: t.reserved_reference and t.reserved_reference == pos_reference)
        if reserved:
            tx = reserved[0]
            match_type = "reference" if tx._text_matches_reference(pos_reference) else "amount"
            tx._link_pos_payment(self, match_type)
            return True
        for tx in candidates:
            if tx._text_matches_reference(pos_reference):
                tx._link_pos_payment(self, "reference")
                return True
        return False

    @api.model
    def fio_pending_overview(self, config_id, limit=50):
        """Pro lištu pokladny: QR platby čekající na banku u objednávek této pokladny.

        Vrací počet a seznam nejnovějších (datum, číslo objednávky, částka) k zobrazení
        v popupu; rozsah dní zpět určuje nastavení pokladny „Nespárované QR platby: dny zpět“.
        """
        config = self.env["pos.config"].browse(config_id).exists()
        config._fio_heartbeat()
        domain = [("fio_state", "=", "pending"), ("pos_order_id.config_id", "=", config.id)]
        if config and config.fio_pending_days > 0:
            since = fields.Datetime.now() - timedelta(days=config.fio_pending_days)
            domain.append(("payment_date", ">=", since))
        payments = self.sudo().search(domain, order="payment_date desc, id desc", limit=limit)
        tip_today, tip_today_count = self._fio_tips_today(config)
        banks = self._fio_banks_for_config(config)
        return {
            "count": self.sudo().search_count(domain),
            "tip_today": tip_today,
            "tip_today_count": tip_today_count,
            "tips_url": f"/odoo/action-{TIPS_ACTION_XMLID}",
            # pro odpočet v popupu: za kolik sekund Fio dovolí další dotaz (0 = hned)
            "next_check_in": max([bank._fio_seconds_until_allowed() for bank in banks], default=0),
            "rate_limit": RATE_LIMIT_SECONDS,
            "bank_enabled": bool(banks),
            "payments": [
                {
                    "id": payment.id,
                    "payment_date": fields.Datetime.to_string(payment.payment_date),
                    "amount": payment.amount,
                    "order_id": payment.pos_order_id.id,
                    "order_name": payment.pos_order_id.name or "",
                    "pos_reference": payment.pos_order_id.pos_reference or "",
                    "tracking_number": payment.pos_order_id.tracking_number or "",
                    "partner_name": payment.pos_order_id.partner_id.name or "",
                }
                for payment in payments
            ],
        }

    @api.model
    def fio_refresh_from_bank(self, config_id):
        """Tlačítko „Načíst platby z banky“ v pokladně: stáhne nové pohyby z Fio pro účty QR metod
        této pokladny (limit 30 s), spáruje čekající platby a vrátí obnovený přehled.

        `synced` – dotaz na Fio proběhl; `wait` – za kolik sekund Fio dovolí další dotaz;
        `error` – poslední chyba Fio (např. neplatný token).
        """
        config = self.env["pos.config"].sudo().browse(config_id).exists()
        result = {"synced": False, "wait": 0, "error": ""}
        if config:
            config._fio_heartbeat()
            for bank in self._fio_banks_for_config(config):
                wait = bank._fio_seconds_until_allowed()
                if wait:
                    result["wait"] = max(result["wait"], wait)
                    continue
                if bank.fio_sync_recent():
                    result["synced"] = True
                elif bank.fio_last_error:
                    result["error"] = bank.fio_last_error
                else:
                    # zarážka `last` se právě nastavila nebo účet dotazuje někdo jiný: zkusit za chvíli
                    result["wait"] = max(result["wait"], bank._fio_seconds_until_allowed() or 30)
            self.env["fio.bank.transaction"].sudo()._match_pending()
        result.update(self.fio_pending_overview(config_id))
        return result

    @api.model
    def _fio_banks_for_config(self, config):
        """Účty s Fio tokenem u QR platebních metod dané pokladny."""
        if not config:
            return self.env["res.partner.bank"].sudo()
        methods = config.sudo().payment_method_ids.filtered(
            lambda m: m.payment_method_type == "qr_code" or m.qr_code_method
        )
        return methods.journal_id.bank_account_id.filtered("fio_api_token")

    @api.model
    def _fio_today_bounds(self):
        """Začátek a konec dnešního dne uživatele (časová zóna z kontextu / uživatele) v UTC."""
        tz = pytz.timezone(self.env.context.get("tz") or self.env.user.tz or "UTC")
        today = fields.Date.context_today(self)
        start = tz.localize(datetime.combine(today, time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        return start, start + timedelta(days=1)

    @api.model
    def _fio_tips_today(self, config):
        """Součet a počet dýšek z QR plateb této pokladny zaplacených dnes (podle dne platby v pokladně)."""
        if not config:
            return 0.0, 0
        start, end = self._fio_today_bounds()
        rows = self.env["fio.bank.transaction"].sudo()._read_group(
            [
                ("tip_amount", ">", 0),
                ("pos_config_id", "=", config.id),
                ("pos_payment_date", ">=", start),
                ("pos_payment_date", "<", end),
            ],
            [],
            ["tip_amount:sum", "__count"],
        )
        total, count = rows[0] if rows else (0.0, 0)
        return total or 0.0, count or 0

    def action_fio_confirm_manual(self):
        """Ručně potvrzeno (zkontrolováno jinak). Hromadně bezpečné: platby potvrzené bankou se nemění."""
        self.filtered(lambda p: p.fio_state in ("pending", "ignored")).write({
            "fio_state": "manual", "fio_confirmed_at": fields.Datetime.now(),
        })
        return True

    def action_fio_ignore(self):
        """Vyřadit z kontroly. Hromadně bezpečné: mění jen platby čekající na banku."""
        self.filtered(lambda p: p.fio_state == "pending").write({"fio_state": "ignored"})
        return True

    def action_fio_reset_pending(self):
        """Vrátit ke kontrole; u platby potvrzené bankou se zruší i spárování pohybu."""
        payments = self.filtered(lambda p: p.fio_state in ("confirmed", "manual", "ignored"))
        for payment in payments:
            if payment.fio_transaction_id:
                payment.fio_transaction_id.sudo().action_reset()
        payments.write({"fio_state": "pending", "fio_transaction_id": False, "fio_confirmed_at": False})
        return True
