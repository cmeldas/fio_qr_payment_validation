# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

import psycopg2

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from . import fio_api

_logger = logging.getLogger(__name__)

# Fio pouští data starší 90 dní jen po silné autorizaci v internetbankingu, a to na 10 minut.
# Proto co nejméně dotazů: roční bloky (limit Fio je 50 000 pohybů na dotaz), mezi nimi 35 s.
HISTORY_CHUNK_DAYS = 365
HISTORY_STEP_DELAY_SECONDS = 35
# Nový token nemá zarážku pro `last` a Fio by sáhlo do celé historie (HTTP 422);
# první stažení proto napřed nastaví zarážku takto daleko zpět (musí být pod 90 dní).
LAST_INIT_DAYS = 85


class ResPartnerBank(models.Model):
    _inherit = "res.partner.bank"

    fio_api_token = fields.Char(
        string="Fio API token",
        groups="base.group_system",
        copy=False,
        help="Token jen pro čtení z internetového bankovnictví Fio (64 znaků). Vyplněný token "
        "zapíná stahování pohybů a párování QR plateb pro tento účet.",
    )
    fio_enabled = fields.Boolean(string="Fio párování zapnuté", compute="_compute_fio_enabled")
    fio_last_request_at = fields.Datetime(string="Poslední dotaz na Fio", readonly=True, copy=False)
    fio_last_sync_at = fields.Datetime(string="Poslední úspěšné stažení", readonly=True, copy=False)
    fio_last_error = fields.Char(string="Poslední chyba Fio", readonly=True, copy=False)
    fio_history_from = fields.Date(
        string="Historie od",
        help="Od kterého dne stáhnout historii pohybů (po 90 dnech, mezi bloky pauza kvůli limitu Fio).",
    )
    fio_history_cursor = fields.Date(string="Historie stažena do", readonly=True, copy=False)
    fio_history_running = fields.Boolean(string="Import historie běží", readonly=True, copy=False)
    fio_history_set_last_id = fields.Char(
        string="Zarážka k nastavení", readonly=True, copy=False,
        help="Interní: po importu historie se na Fio nastaví zarážka `last` za tento pohyb.",
    )
    fio_bookmark_set = fields.Boolean(
        string="Zarážka `last` nastavena", readonly=True, copy=False,
        help="Fio si u tokenu pamatuje, odkud posílat nové pohyby. Bez zarážky by první stažení "
        "sáhlo do celé historie a Fio ho odmítne (HTTP 422); modul ji proto před prvním stažením "
        "nastaví 85 dní zpět, nebo za poslední pohyb po importu historie.",
    )
    fio_transaction_count = fields.Integer(string="Pohybů", compute="_compute_fio_transaction_count")

    @api.depends("fio_api_token")
    def _compute_fio_enabled(self):
        for bank in self:
            bank.fio_enabled = bool(bank.sudo().fio_api_token)

    def _compute_fio_transaction_count(self):
        counts = {
            bank.id: count
            for bank, count in self.env["fio.bank.transaction"]._read_group(
                [("bank_account_id", "in", self.ids)], ["bank_account_id"], ["__count"]
            )
        }
        for bank in self:
            bank.fio_transaction_count = counts.get(bank.id, 0)

    # ------------------------------------------------------------------
    # Limit Fio: jeden dotaz za 30 s, souběh přes zámek řádku účtu
    # ------------------------------------------------------------------
    def _fio_seconds_until_allowed(self):
        self.ensure_one()
        last = self.sudo().fio_last_request_at
        if not last:
            return 0
        elapsed = (fields.Datetime.now() - last).total_seconds()
        remaining = fio_api.RATE_LIMIT_SECONDS - elapsed
        return int(remaining) + 1 if remaining > 0 else 0

    def _fio_try_lock(self):
        """Zamkne řádek účtu do konce transakce; když ho drží souběžný požadavek, vrátí False."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    "SELECT id FROM res_partner_bank WHERE id = %s FOR UPDATE NOWAIT", (self.id,)
                )
        except psycopg2.errors.LockNotAvailable:
            return False
        return True

    def _fio_call(self, func, *args):
        """Zavolá Fio s ohledem na limit.

        Vrací výsledek funkce (True, když funkce nic nevrací), nebo None když dotaz
        neproběhl (limit, chybějící token, chyba Fio).
        """
        self.ensure_one()
        bank = self.sudo()
        token = bank.fio_api_token
        if not token or bank._fio_seconds_until_allowed() > 0:
            return None
        bank.fio_last_request_at = fields.Datetime.now()
        try:
            result = func(token, *args)
        except fio_api.FioRateLimitError as exc:
            bank.fio_last_error = str(exc)
            _logger.warning("Fio %s: %s", self.display_name, exc)
            return None
        except fio_api.FioApiError as exc:
            bank.fio_last_error = str(exc)
            _logger.error("Fio %s: %s", self.display_name, exc)
            return None
        bank.fio_last_error = False
        return True if result is None else result

    # ------------------------------------------------------------------
    # Stahování pohybů
    # ------------------------------------------------------------------
    def _fio_store_transactions(self, items):
        """Uloží dosud neznámé pohyby, vrací nově vytvořené záznamy."""
        self.ensure_one()
        Transaction = self.env["fio.bank.transaction"].sudo()
        if not items:
            return Transaction
        known = set(
            Transaction.search([
                ("bank_account_id", "=", self.id),
                ("fio_id", "in", [item["fio_id"] for item in items]),
            ]).mapped("fio_id")
        )
        vals_list = [Transaction._vals_from_fio(self, item) for item in items if item["fio_id"] not in known]
        created = Transaction.create(vals_list) if vals_list else Transaction
        if created:
            _logger.info("Fio %s: uloženo %s nových pohybů", self.display_name, len(created))
        return created

    def _fio_init_bookmark(self):
        """První kontakt s tokenem: nastaví zarážku `last` 85 dní zpět. Vrací True při úspěchu."""
        self.ensure_one()
        bank = self.sudo()
        since = fields.Date.context_today(self) - timedelta(days=LAST_INIT_DAYS)
        if self._fio_call(fio_api.set_last_date, since) is None:
            return False
        bank.fio_bookmark_set = True
        _logger.info("Fio %s: zarážka `last` nastavena na %s", self.display_name, since)
        return True

    def fio_sync_recent(self):
        """Stáhne nové pohyby (`last`) a spáruje je. Vrací True, když dotaz na Fio proběhl.

        Respektuje limit 30 s; když teď volat nelze nebo účet právě dotazuje jiný požadavek,
        nic nestahuje a vrátí False (párování dál běží nad už uloženými pohyby). U tokenu bez
        zarážky ji napřed nastaví (jeden dotaz) a samotné stažení nechá na příští volání.
        """
        self.ensure_one()
        if not self.sudo().fio_api_token or not self._fio_try_lock():
            return False
        if not self.sudo().fio_bookmark_set:
            self._fio_init_bookmark()
            return False
        items = self._fio_call(fio_api.fetch_last)
        if items is None:
            return False
        created = self._fio_store_transactions(items)
        self.sudo().fio_last_sync_at = fields.Datetime.now()
        created._match_new()
        return True

    def action_fio_fetch_now(self):
        """Tlačítko: stáhnout nové pohyby hned a spárovat vše čekající."""
        for bank in self:
            if not bank.sudo().fio_api_token:
                raise UserError(_("Účet %s nemá vyplněný Fio API token.", bank.display_name))
            wait = bank._fio_seconds_until_allowed()
            if wait:
                raise UserError(_("Fio dovolí jeden dotaz za 30 sekund, zkus to znovu za %s s.", wait))
            if not bank.sudo().fio_bookmark_set:
                if not bank._fio_init_bookmark():
                    raise UserError(bank.sudo().fio_last_error or _("Nastavení zarážky na Fio se nepovedlo, viz log serveru."))
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "type": "info",
                        "title": _("Zarážka nastavena"),
                        "message": _("Fio bude posílat pohyby od %s. Za 30 s klikni znovu na „Načíst nové pohyby“.",
                                     fields.Date.context_today(self) - timedelta(days=LAST_INIT_DAYS)),
                        "sticky": False,
                    },
                }
            if not bank.fio_sync_recent():
                raise UserError(bank.sudo().fio_last_error or _("Stažení z Fio se nepovedlo, viz log serveru."))
        self.env["fio.bank.transaction"].sudo()._match_pending()
        return True

    def action_fio_match_pending(self):
        """Tlačítko: jen spárovat čekající platby s už staženými pohyby (bez dotazu na Fio)."""
        self.env["fio.bank.transaction"].sudo()._match_pending()
        return True

    # ------------------------------------------------------------------
    # Import historie (po blocích přes cron, mezi bloky pauza kvůli limitu)
    # ------------------------------------------------------------------
    def action_fio_import_history(self):
        for bank in self:
            if not bank.sudo().fio_api_token:
                raise UserError(_("Účet %s nemá vyplněný Fio API token.", bank.display_name))
            if not bank.fio_history_from:
                raise UserError(_("Vyplň „Historie od“."))
            wait = bank._fio_seconds_until_allowed()
            if wait:
                raise UserError(_("Fio dovolí jeden dotaz za 30 sekund, zkus to znovu za %s s.", wait))
            bank.sudo().write({
                "fio_history_cursor": bank.fio_history_from - timedelta(days=1),
                "fio_history_running": True,
            })
        self._fio_schedule_history_step(0)
        return True

    def action_fio_stop_history(self):
        self.sudo().write({"fio_history_running": False})
        return True

    @api.model
    def _fio_schedule_history_step(self, delay_seconds=HISTORY_STEP_DELAY_SECONDS):
        self.env.ref("fio_qr_payment_validation.cron_fio_history")._trigger(
            fields.Datetime.now() + timedelta(seconds=delay_seconds)
        )

    @api.model
    def _cron_fio_history_step(self):
        """Jeden blok historie pro každý účet s běžícím importem, potom další naplánování.

        Každý účet udělá nejvýš jeden dotaz na Fio za běh (limit 30 s), proto se cron
        po každém kroku znovu naplánuje za 35 s, dokud je co dělat.
        """
        again = False
        today = fields.Date.context_today(self)
        for bank in self.sudo().search([("fio_history_set_last_id", "!=", False)]):
            if not bank._fio_try_lock():
                again = True
                continue
            if bank._fio_call(fio_api.set_last_id, bank.fio_history_set_last_id):
                bank.write({"fio_history_set_last_id": False, "fio_bookmark_set": True})
                _logger.info("Fio %s: zarážka `last` nastavena po importu historie", bank.display_name)
            elif bank.fio_last_error and "409" not in bank.fio_last_error:
                bank.fio_history_set_last_id = False
                _logger.error("Fio %s: zarážku `last` se nepovedlo nastavit: %s", bank.display_name, bank.fio_last_error)
            else:
                again = True
        for bank in self.sudo().search([("fio_history_running", "=", True)]):
            cursor = bank.fio_history_cursor or (bank.fio_history_from or today) - timedelta(days=1)
            start = cursor + timedelta(days=1)
            if start > today:
                bank._fio_finish_history()
                continue
            end = min(start + timedelta(days=HISTORY_CHUNK_DAYS - 1), today)
            if not bank._fio_try_lock():
                again = True
                continue
            items = bank._fio_call(fio_api.fetch_periods, start, end)
            if items is None:
                if bank.fio_last_error and "409" not in bank.fio_last_error:
                    # jiná chyba než limit (špatný token, odmítnutý požadavek): nezacyklit se
                    bank.fio_history_running = False
                    _logger.error("Fio %s: import historie zastaven: %s", bank.display_name, bank.fio_last_error)
                    continue
                again = True  # limit, zkusit znovu za chvíli
                continue
            bank._fio_store_transactions(items)
            bank.fio_history_cursor = end
            _logger.info("Fio %s: historie %s – %s, %s pohybů", bank.display_name, start, end, len(items))
            if end >= today:
                bank._fio_finish_history()
            again = True
        self.env["fio.bank.transaction"].sudo()._match_pending()
        if again:
            self._fio_schedule_history_step()

    def _fio_finish_history(self):
        """Konec importu: zapamatovat nejnovější pohyb, zarážku `last` nastaví další krok cronu."""
        self.ensure_one()
        bank = self.sudo()
        bank.fio_history_running = False
        bank.fio_last_sync_at = fields.Datetime.now()
        newest = self.env["fio.bank.transaction"].sudo().search(
            [("bank_account_id", "=", self.id)], order="date desc, id desc", limit=200
        )
        if newest:
            bank.fio_history_set_last_id = str(max(int(t.fio_id) for t in newest))
        _logger.info("Fio %s: import historie dokončen", bank.display_name)

    # ------------------------------------------------------------------
    # Hodinový cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_fio_sync(self):
        for bank in self.sudo().search([("fio_api_token", "!=", False), ("fio_history_running", "=", False)]):
            if bank.fio_history_set_last_id:
                continue  # zarážku ještě řeší cron historie
            bank.fio_sync_recent()
        self.env["fio.bank.transaction"].sudo()._match_pending()

    def action_fio_open_transactions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Pohyby Fio"),
            "res_model": "fio.bank.transaction",
            "view_mode": "list,form",
            "domain": [("bank_account_id", "=", self.id)],
            "context": {"search_default_unmatched": 1},
            "target": "current",
        }
