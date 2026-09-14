# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models

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
        domain = [("fio_state", "=", "pending"), ("pos_order_id.config_id", "=", config.id)]
        if config and config.fio_pending_days > 0:
            since = fields.Datetime.now() - timedelta(days=config.fio_pending_days)
            domain.append(("payment_date", ">=", since))
        payments = self.sudo().search(domain, order="payment_date desc, id desc", limit=limit)
        return {
            "count": self.sudo().search_count(domain),
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

    def action_fio_confirm_manual(self):
        self.write({"fio_state": "manual", "fio_confirmed_at": fields.Datetime.now()})
        return True

    def action_fio_ignore(self):
        self.write({"fio_state": "ignored"})
        return True

    def action_fio_reset_pending(self):
        for payment in self:
            if payment.fio_transaction_id:
                payment.fio_transaction_id.sudo().action_reset()
        self.write({"fio_state": "pending", "fio_transaction_id": False, "fio_confirmed_at": False})
        return True
