# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = "account.payment"

    fio_transaction_id = fields.Many2one("fio.bank.transaction", string="Pohyb Fio", copy=False, readonly=True)
