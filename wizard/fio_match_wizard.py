# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class FioMatchWizard(models.TransientModel):
    _name = "fio.match.wizard"
    _description = "Ruční spárování pohybu Fio"

    transaction_id = fields.Many2one("fio.bank.transaction", string="Pohyb", required=True, readonly=True)
    amount = fields.Monetary(related="transaction_id.amount", currency_field="currency_id")
    currency_id = fields.Many2one(related="transaction_id.currency_id")
    target = fields.Selection(
        [("pos", "Platba v pokladně"), ("invoice", "Faktura")], string="Spárovat s", default="pos", required=True,
    )
    pos_payment_id = fields.Many2one(
        "pos.payment", string="Platba v pokladně",
        domain="[('fio_state', 'in', ('pending', 'manual'))]",
    )
    move_id = fields.Many2one(
        "account.move", string="Faktura",
        domain="[('move_type', 'in', ('out_invoice', 'out_receipt')), ('state', '=', 'posted')]",
    )
    register_payment = fields.Boolean(
        string="Zaregistrovat platbu na fakturu", default=True,
        help="Vytvoří platbu v bankovním deníku a spáruje ji s fakturou (jen u neuhrazené faktury).",
    )

    @api.onchange("transaction_id")
    def _onchange_transaction_id(self):
        """Předvyplní nejbližší kandidáty: čekající QR platbu ve stejné částce, fakturu podle VS."""
        tx = self.transaction_id
        if not tx:
            return
        candidates = tx._pos_candidates()
        if len(candidates) == 1:
            self.pos_payment_id = candidates
        move = tx._find_invoices_by_vs(tx.variable_symbol)[:1]
        if move:
            self.move_id = move
            if not candidates:
                self.target = "invoice"

    def action_confirm(self):
        self.ensure_one()
        tx = self.transaction_id.sudo()
        if tx.state == "matched":
            raise UserError(_("Pohyb je už spárovaný, nejdřív zruš spárování."))
        if self.target == "pos":
            if not self.pos_payment_id:
                raise UserError(_("Vyber platbu v pokladně."))
            tx._link_pos_payment(self.pos_payment_id, "manual")
        else:
            if not self.move_id:
                raise UserError(_("Vyber fakturu."))
            payment = None
            if self.register_payment and self.move_id.payment_state in ("not_paid", "partial"):
                payment = tx._register_invoice_payment(self.move_id)
            tx._link_invoice(self.move_id, "manual", payment=payment)
        return {"type": "ir.actions.act_window_close"}
