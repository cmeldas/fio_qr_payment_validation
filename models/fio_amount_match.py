# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import SQL

from .fio_bank_transaction import AMOUNT_TOLERANCE
from .pos_payment import FIO_STATES

# Kandidáti s dýškem: pohyb nejvýš o tuto část vyšší než platba (výchozí filtr ukazuje jen přesnou částku).
TIP_CANDIDATE_RATIO = 0.5
# Okno kandidátů podle dne v bance: od 2 dnů před dnem objednávky (zákazník poslal peníze předem)
# do 30 dnů po něm (zaplatil později); starší a pozdější pohyby jsou jen šum.
DAYS_BEFORE_ORDER = 2
DAYS_AFTER_ORDER = 30
# Fio posílá jen den zaúčtování (v pražském čase), bez času; čas příchodu odhaduje okamžik stažení.
FIO_TIMEZONE = "Europe/Prague"
# id dvojice = id platby × ID_FACTOR + id pohybu: stabilní mezi dotazy, takže tlačítko na řádku
# vždy spáruje tu dvojici, kterou obsluha viděla (pořadí řádků se mezitím může změnit)
ID_FACTOR = 1_000_000_000


class FioAmountMatch(models.Model):
    """Ruční párování podle částky: dvojice nespárovaná QR platba × nespárovaný příchozí pohyb.

    Počítá se živě z databáze (bez tabulky). Automaticky se podle částky nepáruje nikdy
    (20.0.1.2.2); tady obsluha sama posoudí, jestli k sobě platba a pohyb patří, podle toho,
    kolik hodin po objednávce peníze přišly, od koho a co si zákazník objednal.
    """

    _name = "fio.amount.match"
    _description = "Spárování QR platby podle částky"
    _auto = False
    _order = "payment_date desc, delay_abs, id"

    payment_id = fields.Many2one("pos.payment", string="Platba v pokladně", readonly=True)
    transaction_id = fields.Many2one("fio.bank.transaction", string="Pohyb Fio", readonly=True)
    pos_order_id = fields.Many2one("pos.order", string="Objednávka", readonly=True)
    pos_config_id = fields.Many2one("pos.config", string="Pokladna", readonly=True)
    pos_reference = fields.Char(string="Číslo objednávky", readonly=True)
    currency_id = fields.Many2one("res.currency", string="Měna", readonly=True)
    payment_date = fields.Datetime(string="Objednáno", readonly=True)
    payment_amount = fields.Monetary(string="Platba", currency_field="currency_id", readonly=True)
    payment_state = fields.Selection(FIO_STATES, string="Ověření Fio", readonly=True)
    tx_date = fields.Date(string="Datum v bance", readonly=True)
    tx_amount = fields.Monetary(string="Přišlo", currency_field="currency_id", readonly=True)
    tip_amount = fields.Monetary(string="Dýško", currency_field="currency_id", readonly=True)
    is_exact = fields.Boolean(string="Přesná částka", readonly=True)
    arrived_at = fields.Datetime(
        string="Peníze v Odoo", readonly=True,
        help="Fio neposílá čas pohybu, jen den. Tady je okamžik, kdy Odoo pohyb stáhlo: při běžící "
        "pokladně do pár minut po připsání, jinak do hodiny; nejpozději konec dne zaúčtování.",
    )
    delay_hours = fields.Float(
        string="Po objednávce", readonly=True,
        help="Kolik hodin po zadání platby v pokladně peníze přišly (podle sloupce Peníze v Odoo). "
        "Záporné číslo: pohyb přišel dřív, než vznikla objednávka. Čím dál od nuly, tím méně "
        "pravděpodobné, že k sobě patří.",
    )
    delay_abs = fields.Float(string="Odstup (h)", readonly=True)
    counter_name = fields.Char(string="Plátce", readonly=True)
    counter_account = fields.Char(string="Protiúčet", readonly=True)
    message = fields.Char(string="Zpráva pro příjemce", readonly=True)
    transaction_type = fields.Char(string="Typ pohybu", readonly=True)
    order_summary = fields.Char(string="Objednané zboží", compute="_compute_order_summary")

    _depends = {
        "pos.payment": ["amount", "fio_state", "payment_date", "payment_method_id", "pos_order_id"],
        "pos.order": ["pos_reference", "config_id"],
        "pos.payment.method": ["journal_id"],
        "account.journal": ["bank_account_id"],
        "fio.bank.transaction": [
            "state", "amount", "date", "bank_account_id", "currency_id", "counter_name",
            "counter_account", "message", "transaction_type",
        ],
    }

    @property
    def _table_sql(self):
        to_flush = [
            self.env[model]._fields[name] for model, names in self._depends.items() for name in names
        ]
        return SQL(
            """(
            SELECT p.id::bigint * %(factor)s + t.id AS id,
                   p.id AS payment_id,
                   t.id AS transaction_id,
                   p.pos_order_id,
                   o.config_id AS pos_config_id,
                   o.pos_reference,
                   t.currency_id,
                   p.payment_date,
                   p.amount AS payment_amount,
                   p.fio_state AS payment_state,
                   t.date AS tx_date,
                   t.amount AS tx_amount,
                   t.amount - p.amount AS tip_amount,
                   abs(t.amount - p.amount) < %(tolerance)s AS is_exact,
                   arrival.arrived_at,
                   extract(epoch FROM arrival.arrived_at - p.payment_date) / 3600.0 AS delay_hours,
                   abs(extract(epoch FROM arrival.arrived_at - p.payment_date) / 3600.0) AS delay_abs,
                   t.counter_name,
                   t.counter_account,
                   t.message,
                   t.transaction_type
              FROM pos_payment p
              JOIN pos_order o ON o.id = p.pos_order_id
              JOIN pos_payment_method pm ON pm.id = p.payment_method_id
              LEFT JOIN account_journal j ON j.id = pm.journal_id
              JOIN fio_bank_transaction t
                ON t.state = 'new'
               AND t.amount > 0
               AND t.amount >= p.amount - %(tolerance)s
               AND t.amount <= p.amount * %(max_ratio)s + %(tolerance)s
               AND (j.bank_account_id IS NULL OR j.bank_account_id = t.bank_account_id)
               AND t.date BETWEEN ((p.payment_date AT TIME ZONE 'UTC') AT TIME ZONE %(tz)s)::date - %(days_before)s
                              AND ((p.payment_date AT TIME ZONE 'UTC') AT TIME ZONE %(tz)s)::date + %(days_after)s
             CROSS JOIN LATERAL (
                   SELECT least(
                       t.create_date,
                       ((t.date + 1)::timestamp AT TIME ZONE %(tz)s) AT TIME ZONE 'UTC'
                   ) AS arrived_at
             ) arrival
             WHERE p.fio_state IN ('pending', 'manual')
               AND p.amount > 0
            )""",
            factor=ID_FACTOR,
            tolerance=AMOUNT_TOLERANCE,
            max_ratio=1 + TIP_CANDIDATE_RATIO,
            tz=FIO_TIMEZONE,
            days_before=DAYS_BEFORE_ORDER,
            days_after=DAYS_AFTER_ORDER,
            to_flush=to_flush,
        )

    def _compute_order_summary(self):
        for rec in self:
            parts = [
                f"{line.qty:g} {line.product_uom_id.name or ''} {line.full_product_name or line.product_id.display_name}"
                .replace("  ", " ")
                for line in rec.pos_order_id.lines
            ]
            summary = ", ".join(parts)
            rec.order_summary = summary if len(summary) <= 120 else summary[:117] + "…"

    def _check_can_match(self):
        user = self.env.user
        if not (user.has_group("point_of_sale.group_pos_manager") or user.has_group("account.group_account_manager")):
            raise AccessError(_("Párovat platby s pohyby smí jen správce pokladny nebo účetní."))

    def action_open_pos_order(self):
        """Objednávka, ke které platba patří (co si zákazník objednal)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "pos.order",
            "res_id": self.pos_order_id.id,
            "views": [(False, "form")],
            "target": "current",
        }

    def action_open_transaction(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "fio.bank.transaction",
            "res_id": self.transaction_id.id,
            "views": [(False, "form")],
            "target": "current",
        }

    def action_match(self):
        """Spáruje vybrané dvojice (Ručně). Každá platba i pohyb smí být ve výběru jen jednou
        a obojí musí být pořád nespárované, jinak se nespáruje nic."""
        self._check_can_match()
        if len(self.payment_id) != len(self) or len(self.transaction_id) != len(self):
            raise UserError(_(
                "Každá platba i každý pohyb smí být ve výběru jen jednou: jeden pohyb patří k jedné platbě."
            ))
        for rec in self:
            payment, tx = rec.payment_id.sudo(), rec.transaction_id.sudo()
            if (
                payment.fio_state not in ("pending", "manual")
                or tx.state != "new"
                or tx.amount < payment.amount - AMOUNT_TOLERANCE
            ):
                raise UserError(_(
                    "Platba %(order)s nebo pohyb %(tx)s se mezitím změnily (spárování, ignorování). "
                    "Obnov seznam a vyber znovu.",
                    order=rec.pos_reference or rec.pos_order_id.display_name, tx=tx.display_name,
                ))
        for rec in self:
            rec.transaction_id.sudo()._link_pos_payment(
                rec.payment_id.sudo(), "manual", note=_("Spárováno ručně podle částky."),
            )
        return True
