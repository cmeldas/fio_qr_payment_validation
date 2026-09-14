# -*- coding: utf-8 -*-
from odoo import api, fields, models

FIO_STATES = [
    ("none", "Bez ověření"),
    ("pending", "Čeká na banku"),
    ("confirmed", "Potvrzeno bankou"),
    ("manual", "Potvrzeno ručně"),
    ("ignored", "Ignorováno"),
]


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

    @api.model_create_multi
    def create(self, vals_list):
        payments = super().create(vals_list)
        for payment in payments:
            if payment.fio_state == "none" and payment._is_fio_qr_payment() and not payment.is_change:
                payment.fio_state = "pending"
                payment._fio_try_match()
        return payments

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
