# -*- coding: utf-8 -*-
import logging
import re
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare

from .fio_api import normalize_text

_logger = logging.getLogger(__name__)

AMOUNT_TOLERANCE = 0.005
MIN_REFERENCE_KEY_LENGTH = 6


def digits(value):
    return re.sub(r"\D", "", value or "")


class FioBankTransaction(models.Model):
    _name = "fio.bank.transaction"
    _description = "Pohyb na účtu Fio"
    _order = "date desc, id desc"

    bank_account_id = fields.Many2one(
        "res.partner.bank", string="Náš účet", required=True, ondelete="cascade", index=True,
    )
    fio_id = fields.Char(string="Fio ID pohybu", required=True, index=True)
    date = fields.Date(string="Datum", index=True)
    amount = fields.Monetary(string="Částka", currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", string="Měna")
    counter_account = fields.Char(string="Protiúčet")
    counter_bank_code = fields.Char(string="Kód banky")
    counter_name = fields.Char(string="Název protiúčtu")
    variable_symbol = fields.Char(string="VS", index=True)
    constant_symbol = fields.Char(string="KS")
    specific_symbol = fields.Char(string="SS")
    user_identification = fields.Char(string="Identifikace")
    message = fields.Char(string="Zpráva pro příjemce")
    specification = fields.Char(string="Upřesnění")
    transaction_type = fields.Char(string="Typ")
    comment = fields.Char(string="Komentář")
    bic = fields.Char(string="BIC")
    raw_json = fields.Json(string="Původní data")

    state = fields.Selection(
        [("new", "Nespárováno"), ("matched", "Spárováno"), ("ignored", "Ignorováno")],
        string="Stav", default="new", required=True, index=True,
    )
    match_type = fields.Selection(
        [
            ("reference", "Číslo objednávky ve zprávě"),
            ("variable_symbol", "Variabilní symbol faktury"),
            ("amount", "Jen částka (platba právě probíhala)"),
            ("manual", "Ručně"),
        ],
        string="Způsob spárování",
    )
    pos_payment_id = fields.Many2one("pos.payment", string="Platba v pokladně", ondelete="set null", index=True)
    pos_order_id = fields.Many2one(related="pos_payment_id.pos_order_id", string="Objednávka")
    move_id = fields.Many2one("account.move", string="Faktura", ondelete="set null", index=True)
    payment_id = fields.Many2one("account.payment", string="Zaregistrovaná platba", ondelete="set null")
    reserved_reference = fields.Char(
        string="Rezervováno pro objednávku", index=True,
        help="Pokladna pohyb rozpoznala při otevřeném QR kódu; po uložení objednávky se spáruje.",
    )
    note = fields.Char(string="Poznámka")
    is_incoming = fields.Boolean(string="Příchozí", compute="_compute_is_incoming", store=True)

    _fio_id_unique = models.Constraint(
        "unique (bank_account_id, fio_id)", "Pohyb s tímto Fio ID už existuje.",
    )

    @api.depends("amount")
    def _compute_is_incoming(self):
        for tx in self:
            tx.is_incoming = tx.amount > 0

    @api.depends("date", "amount", "currency_id", "counter_name", "counter_account")
    def _compute_display_name(self):
        for tx in self:
            who = tx.counter_name or tx.counter_account or _("neznámý plátce")
            tx.display_name = f"{tx.date or ''} {tx.amount:.2f} {tx.currency_id.name or ''} {who}".strip()

    # ------------------------------------------------------------------
    # Vytvoření z dat Fio
    # ------------------------------------------------------------------
    @api.model
    def _vals_from_fio(self, bank_account, item):
        currency = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", item.get("currency") or "CZK")], limit=1
        )
        return {
            "bank_account_id": bank_account.id,
            "fio_id": item["fio_id"],
            "date": item.get("date"),
            "amount": item.get("amount") or 0.0,
            "currency_id": currency.id,
            "counter_account": item.get("counter_account"),
            "counter_bank_code": item.get("counter_bank_code"),
            "counter_name": item.get("counter_name"),
            "variable_symbol": item.get("variable_symbol"),
            "constant_symbol": item.get("constant_symbol"),
            "specific_symbol": item.get("specific_symbol"),
            "user_identification": item.get("user_identification"),
            "message": item.get("message"),
            "specification": item.get("specification"),
            "transaction_type": item.get("transaction_type"),
            "comment": item.get("comment"),
            "bic": item.get("bic"),
            "raw_json": item.get("raw"),
        }

    # ------------------------------------------------------------------
    # Párování
    # ------------------------------------------------------------------
    def _search_text(self):
        self.ensure_one()
        return normalize_text(
            " ".join(filter(None, [self.message, self.user_identification, self.comment, self.specification]))
        )

    @api.model
    def _pos_reference_keys(self, pos_reference):
        """Klíče, podle kterých se číslo objednávky hledá ve zprávě pro příjemce.

        Celé číslo bez mezer („objednávka00275-006-0114“, „26a-1-000042“) a jeho poslední
        část za mezerou („00275-006-0114“) – pokladna 18 posílala „Objednávka …“, 19 posílá
        „Order …“, plátci občas přepíšou začátek zprávy.
        """
        keys = set()
        full = normalize_text(pos_reference)
        if len(full) >= MIN_REFERENCE_KEY_LENGTH:
            keys.add(full)
        tail = normalize_text((pos_reference or "").split()[-1] if pos_reference else "")
        if len(tail) >= MIN_REFERENCE_KEY_LENGTH:
            keys.add(tail)
        return keys

    def _text_matches_reference(self, pos_reference):
        """Číslo objednávky je v textu a nepředchází mu číslice („61-1-000005“ nesedí na „261-1-000005“)."""
        self.ensure_one()
        text = self._search_text()
        return any(
            re.search(r"(?<!\d)" + re.escape(key), text) for key in self._pos_reference_keys(pos_reference)
        )

    def _amount_equals(self, amount):
        self.ensure_one()
        rounding = self.currency_id.rounding or 0.01
        return float_compare(self.amount, amount, precision_rounding=rounding) == 0

    def _match_new(self):
        """Spáruje nové příchozí pohyby s čekajícími QR platbami a fakturami."""
        for tx in self.filtered(lambda t: t.state == "new" and t.amount > 0):
            if not tx._try_match_pos():
                tx._try_match_invoice()

    @api.model
    def _match_pending(self):
        """Projde všechny nespárované příchozí pohyby (cron, tlačítko, po importu historie)."""
        self.search([("state", "=", "new"), ("amount", ">", 0)])._match_new()

    def _pos_candidates(self):
        """Čekající QR platby ve stejné částce vedené na náš účet."""
        self.ensure_one()
        Payment = self.env["pos.payment"].sudo()
        candidates = Payment.search([
            ("fio_state", "in", ("pending", "manual")),
            ("amount", ">=", self.amount - AMOUNT_TOLERANCE),
            ("amount", "<=", self.amount + AMOUNT_TOLERANCE),
        ])
        return candidates.filtered(
            lambda p: not p.payment_method_id.journal_id.bank_account_id
            or p.payment_method_id.journal_id.bank_account_id == self.bank_account_id
        )

    def _try_match_pos(self):
        self.ensure_one()
        candidates = self._pos_candidates()
        if self.reserved_reference:
            reserved = candidates.filtered(lambda p: p.pos_order_id.pos_reference == self.reserved_reference)
            if reserved:
                payment = reserved.sorted("id")[0]
                match_type = "reference" if self._text_matches_reference(payment.pos_order_id.pos_reference) else "amount"
                self._link_pos_payment(payment, match_type)
                return True
        for payment in candidates.sorted(lambda p: p.payment_date or fields.Datetime.now(), reverse=True):
            if self._text_matches_reference(payment.pos_order_id.pos_reference):
                self._link_pos_payment(payment, "reference")
                return True
        return False

    def _link_pos_payment(self, payment, match_type, note=None):
        self.ensure_one()
        self.write({
            "state": "matched",
            "match_type": match_type,
            "pos_payment_id": payment.id,
            "reserved_reference": False,
            "note": note or False,
        })
        payment.sudo().write({
            "fio_state": "confirmed",
            "fio_transaction_id": self.id,
            "fio_confirmed_at": fields.Datetime.now(),
        })
        _logger.info("Fio: pohyb %s spárován s POS platbou %s (%s)", self.fio_id, payment.id, match_type)

    @api.model
    def _find_invoices_by_vs(self, variable_symbol):
        """Zaúčtované vydané faktury, jejichž platební reference bez nečíslic je daný VS
        („FA/2026/00008“ → 202600008)."""
        vs = digits(variable_symbol)
        if not vs:
            return self.env["account.move"].sudo()
        self.env.cr.execute(
            r"""
            SELECT id FROM account_move
             WHERE move_type IN ('out_invoice', 'out_receipt') AND state = 'posted'
               AND payment_reference IS NOT NULL
               AND regexp_replace(payment_reference, '\D', '', 'g') = %s
             ORDER BY id
            """,
            (vs,),
        )
        return self.env["account.move"].sudo().browse([row[0] for row in self.env.cr.fetchall()])

    def _try_match_invoice(self):
        self.ensure_one()
        moves = self._find_invoices_by_vs(self.variable_symbol)
        if not moves:
            return False
        unpaid = moves.filtered(lambda m: m.payment_state in ("not_paid", "partial"))
        move = (unpaid or moves).sorted("id")[0]
        if not unpaid:
            self._link_invoice(move, "variable_symbol", note=_("Faktura už byla uhrazena jinak."))
            return True
        if self.currency_id and move.currency_id != self.currency_id:
            self._link_invoice(move, "variable_symbol", note=_("Jiná měna než faktura, platba neregistrována."))
            return True
        payment = self._register_invoice_payment(move)
        self._link_invoice(move, "variable_symbol", payment=payment)
        return True

    def _link_invoice(self, move, match_type, payment=None, note=None):
        self.ensure_one()
        self.write({
            "state": "matched",
            "match_type": match_type,
            "move_id": move.id,
            "payment_id": payment.id if payment else False,
            "note": note or False,
        })
        _logger.info("Fio: pohyb %s spárován s fakturou %s (%s)", self.fio_id, move.name, match_type)

    def _bank_journal(self, company):
        self.ensure_one()
        Journal = self.env["account.journal"].sudo()
        journal = Journal.search([
            ("type", "=", "bank"),
            ("bank_account_id", "=", self.bank_account_id.id),
            ("company_id", "=", company.id),
        ], limit=1)
        return journal or Journal.search([("type", "=", "bank"), ("company_id", "=", company.id)], limit=1)

    def _register_invoice_payment(self, move):
        """Zaregistruje na faktuře platbu v částce pohybu do bankovního deníku našeho účtu."""
        self.ensure_one()
        journal = self._bank_journal(move.company_id)
        if not journal:
            raise UserError(_("Pro účet %s neexistuje bankovní deník.", self.bank_account_id.display_name))
        wizard = self.env["account.payment.register"].sudo().with_context(
            active_model="account.move", active_ids=move.ids,
        ).create({
            "journal_id": journal.id,
            "amount": self.amount,
            "payment_date": self.date or fields.Date.context_today(self),
            "communication": move.payment_reference or move.name,
        })
        payments = wizard._create_payments()
        payments.sudo().write({"fio_transaction_id": self.id})
        return payments[:1]

    # ------------------------------------------------------------------
    # Akce v přehledu
    # ------------------------------------------------------------------
    def action_ignore(self):
        for tx in self:
            if tx.state == "matched":
                raise UserError(_("Spárovaný pohyb nelze ignorovat, nejdřív zruš spárování."))
        self.write({"state": "ignored", "reserved_reference": False})
        return True

    def action_reset(self):
        """Zruší spárování / ignorování; POS platba se vrátí do čekání, registrovaná platba zůstává."""
        for tx in self:
            if tx.pos_payment_id and tx.pos_payment_id.fio_transaction_id == tx:
                tx.pos_payment_id.sudo().write({"fio_state": "pending", "fio_transaction_id": False, "fio_confirmed_at": False})
        self.write({
            "state": "new", "match_type": False, "pos_payment_id": False, "move_id": False,
            "payment_id": False, "note": False, "reserved_reference": False,
        })
        return True

    def action_open_match_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Ručně spárovat pohyb"),
            "res_model": "fio.match.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_transaction_id": self.id},
        }

    def action_open_pos_order(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "pos.order",
            "res_id": self.pos_order_id.id,
            "view_mode": "form",
            "target": "current",
        }
