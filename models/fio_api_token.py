# -*- coding: utf-8 -*-
"""Tokeny Fio API k bankovnímu účtu.

Fio dovolí jeden dotaz za 30 s na token a každý token má vlastní zarážku „od posledního
stažení“ (`last`). Víc tokenů k jednomu účtu tedy znamená častější dotazy: účet je volá
postupně za sebou (vždy ten, který se nejdéle nepoužil) s intervalem 30 s / počet tokenů.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import fio_api


class FioApiToken(models.Model):
    _name = "fio.api.token"
    _description = "Fio API token"
    _order = "sequence, id"

    bank_account_id = fields.Many2one(
        "res.partner.bank", string="Bankovní účet", required=True, ondelete="cascade", index=True,
    )
    name = fields.Char(string="Název", help="Pro rozlišení tokenů, třeba podle data vydání v bankovnictví.")
    sequence = fields.Integer(string="Pořadí", default=10, help="Import historie používá první token v pořadí.")
    active = fields.Boolean(string="Aktivní", default=True)
    token = fields.Char(
        string="Token", required=True, copy=False, groups="base.group_system",
        help="Token jen pro čtení (sledování účtu) z internetového bankovnictví Fio, Nastavení › API, 64 znaků.",
    )
    token_hint = fields.Char(string="Konec tokenu", compute="_compute_token_hint", compute_sudo=True)
    last_request_at = fields.Datetime(string="Poslední dotaz", readonly=True, copy=False)
    bookmark_set = fields.Boolean(
        string="Zarážka nastavena", readonly=True, copy=False,
        help="Fio si u každého tokenu pamatuje, odkud posílat nové pohyby. Nový token si ji při prvním "
        "použití nastaví 85 dní zpět (bez ní by Fio sáhlo do celé historie a vrátilo HTTP 422).",
    )
    last_error = fields.Char(string="Poslední chyba", readonly=True, copy=False)

    _token_unique = models.Constraint(
        "unique (bank_account_id, token)", "Tento token už je u účtu zadaný.",
    )

    @api.depends("token")
    def _compute_token_hint(self):
        for record in self:
            record.token_hint = f"…{record.token[-4:]}" if record.token else ""

    @api.constrains("token")
    def _check_token(self):
        for record in self.sudo():
            if record.token and (len(record.token) < 20 or any(ch.isspace() for ch in record.token)):
                raise ValidationError(_("Fio API token má 64 znaků bez mezer; zkontroluj, že je zkopírovaný celý."))

    @api.model
    def _clean_vals(self, vals):
        if vals.get("token"):
            vals["token"] = "".join(vals["token"].split())
            # nový token = nová zarážka a čistý stav
            vals.setdefault("bookmark_set", False)
            vals.setdefault("last_error", False)
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._clean_vals(dict(vals)) for vals in vals_list])

    def write(self, vals):
        return super().write(self._clean_vals(dict(vals)))

    def _seconds_until_allowed(self):
        """Za kolik sekund Fio dovolí tímto tokenem další dotaz (0 = hned)."""
        self.ensure_one()
        if not self.last_request_at:
            return 0
        remaining = fio_api.RATE_LIMIT_SECONDS - (fields.Datetime.now() - self.last_request_at).total_seconds()
        return int(remaining) + 1 if remaining > 0 else 0

    def _display(self):
        self.ensure_one()
        return self.name or self.token_hint or _("token %s", self.id)
