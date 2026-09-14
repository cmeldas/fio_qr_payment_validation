# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    fio_transaction_ids = fields.One2many("fio.bank.transaction", "move_id", string="Pohyby Fio")
    fio_transaction_count = fields.Integer(compute="_compute_fio_transaction_count")

    def _compute_fio_transaction_count(self):
        for move in self:
            move.fio_transaction_count = len(move.fio_transaction_ids)

    def action_view_fio_transactions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Pohyby Fio",
            "res_model": "fio.bank.transaction",
            "view_mode": "list,form",
            "domain": [("move_id", "=", self.id)],
            "target": "current",
        }
