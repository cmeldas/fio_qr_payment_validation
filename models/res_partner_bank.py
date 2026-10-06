# -*- coding: utf-8 -*-
import logging
import math
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
# Cron běží každých 5 minut; když žádná pokladna neběží (uspaný notebook), stačí stahovat po hodině.
IDLE_SYNC_MINUTES = 60
# Víc tokenů = častější dotazy (30 s / počet tokenů), ale ne častěji než takhle.
MIN_INTERVAL_SECONDS = 2


class ResPartnerBank(models.Model):
    _inherit = "res.partner.bank"

    fio_token_ids = fields.One2many(
        "fio.api.token", "bank_account_id", string="Fio API tokeny", context={"active_test": False},
        help="Tokeny jen pro čtení z internetového bankovnictví Fio. Aspoň jeden aktivní token zapíná "
        "stahování pohybů a párování QR plateb; víc tokenů zkrátí interval mezi dotazy.",
    )
    fio_enabled = fields.Boolean(
        string="Fio párování zapnuté", compute="_compute_fio_enabled", store=True, compute_sudo=True,
    )
    fio_token_count = fields.Integer(string="Aktivních tokenů", compute="_compute_fio_interval", compute_sudo=True)
    fio_interval = fields.Integer(
        string="Interval mezi dotazy (s)", compute="_compute_fio_interval", compute_sudo=True,
        help="Fio dovolí jeden dotaz za 30 s na token; účet volá tokeny postupně za sebou, "
        "takže interval je 30 s děleno počtem aktivních tokenů (nejméně 2 s).",
    )
    fio_last_request_at = fields.Datetime(string="Poslední dotaz na Fio", readonly=True, copy=False)
    fio_last_sync_at = fields.Datetime(string="Poslední úspěšné stažení", readonly=True, copy=False)
    fio_last_error = fields.Char(string="Poslední chyba Fio", readonly=True, copy=False)
    fio_history_from = fields.Date(
        string="Historie od",
        help="Od kterého dne stáhnout historii pohybů (po 90 dnech, mezi bloky pauza kvůli limitu Fio).",
    )
    fio_history_cursor = fields.Date(string="Historie stažena do", readonly=True, copy=False)
    fio_history_running = fields.Boolean(string="Import historie běží", readonly=True, copy=False)
    fio_transaction_count = fields.Integer(string="Pohybů", compute="_compute_fio_transaction_count")

    @api.depends("fio_token_ids.active", "fio_token_ids.token")
    def _compute_fio_enabled(self):
        for bank in self:
            bank.fio_enabled = bool(bank._fio_tokens())

    @api.depends("fio_token_ids.active", "fio_token_ids.token")
    def _compute_fio_interval(self):
        for bank in self:
            count = len(bank._fio_tokens())
            bank.fio_token_count = count
            bank.fio_interval = bank._fio_interval_for(count)

    @staticmethod
    def _fio_interval_for(token_count):
        """Sekundy mezi dvěma dotazy na účet při daném počtu aktivních tokenů."""
        if token_count <= 0:
            return fio_api.RATE_LIMIT_SECONDS
        return max(MIN_INTERVAL_SECONDS, math.ceil(fio_api.RATE_LIMIT_SECONDS / token_count))

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
    # Limit Fio: jeden dotaz za 30 s na token; víc tokenů se střídá.
    # Souběh (pokladny, cron, tlačítka) řeší zámek řádku účtu.
    # ------------------------------------------------------------------
    def _fio_tokens(self):
        """Aktivní tokeny účtu s vyplněnou hodnotou, v pořadí (sudo: hodnota tokenu je jen pro správce)."""
        self.ensure_one()
        tokens = self.sudo().fio_token_ids.filtered(lambda t: t.active and t.token)
        return tokens.sorted(lambda t: (t.sequence, t.id))

    def _fio_interval_seconds(self):
        self.ensure_one()
        return self._fio_interval_for(len(self._fio_tokens()))

    def _fio_spacing_wait(self):
        """Sekundy do dalšího dotazu podle rovnoměrného rozložení mezi tokeny (30 s / počet)."""
        self.ensure_one()
        last = self.sudo().fio_last_request_at
        if not last:
            return 0
        remaining = self._fio_interval_seconds() - (fields.Datetime.now() - last).total_seconds()
        return int(remaining) + 1 if remaining > 0 else 0

    def _fio_seconds_until_allowed(self):
        """Za kolik sekund jde na účet poslat další dotaz: rozestup mezi dotazy i limit 30 s
        nejdřív volného tokenu. Bez tokenu vrací plný limit."""
        self.ensure_one()
        tokens = self._fio_tokens()
        if not tokens:
            return fio_api.RATE_LIMIT_SECONDS
        return max(self._fio_spacing_wait(), min(t._seconds_until_allowed() for t in tokens))

    def _fio_pick_token(self):
        """Token pro další dotaz: ten, který se nejdéle nepoužil, pokud už smí a rozestup to dovolí."""
        self.ensure_one()
        tokens = self._fio_tokens()
        if not tokens or self._fio_spacing_wait() > 0:
            return tokens.browse()
        free = tokens.filtered(lambda t: t._seconds_until_allowed() == 0)
        never_used = free.filtered(lambda t: not t.last_request_at)
        if never_used:
            return never_used[:1]
        return free.sorted(lambda t: (t.last_request_at, t.sequence, t.id))[:1]

    def _fio_history_token(self):
        """Token pro import historie: první v pořadí (odemčení zámkem v bankovnictví platí pro jeden token)."""
        self.ensure_one()
        return self._fio_tokens()[:1]

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

    def _fio_call(self, func, *args, token=None):
        """Zavolá Fio jedním tokenem s ohledem na limit.

        Bez `token` vybere ten, který se nejdéle nepoužil, a dodrží rozestup mezi dotazy;
        se zadaným tokenem hlídá jen jeho vlastních 30 s. Vrací výsledek funkce (True, když
        funkce nic nevrací), nebo None, když dotaz neproběhl (limit, žádný token, chyba Fio).
        Chyba se uloží k tokenu i k účtu. HTTP 422 u stažení `last` zruší zarážku tokenu
        (je starší než 90 dní), takže si ji token příště nastaví znovu.
        """
        self.ensure_one()
        bank = self.sudo()
        if token is None:
            token = bank._fio_pick_token()
        else:
            token = token.sudo()
            if token._seconds_until_allowed() > 0:
                token = token.browse()
        if not token:
            return None
        now = fields.Datetime.now()
        token.last_request_at = now
        bank.fio_last_request_at = now
        label = token._display()
        try:
            result = func(token.token, *args)
        except fio_api.FioRateLimitError as exc:
            token.last_error = str(exc)
            bank.fio_last_error = f"{label}: {exc}"
            _logger.warning("Fio %s (%s): %s", self.display_name, label, exc)
            return None
        except fio_api.FioApiError as exc:
            token.last_error = str(exc)
            if isinstance(exc, fio_api.FioHistoryLockedError) and func is fio_api.fetch_last:
                token.bookmark_set = False
            bank.fio_last_error = f"{label}: {exc}"
            _logger.error("Fio %s (%s): %s", self.display_name, label, exc)
            return None
        token.last_error = False
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

    def _fio_sync_recent(self):
        """Jeden krok stahování nových pohybů (`last`) dalším tokenem v pořadí.

        Vrací stav: "synced" (staženo a spárováno), "bookmark" (token neměl zarážku, tento
        dotaz ji nastavil 85 dní zpět, stahovat bude příště), "wait" (limit nebo souběžný
        požadavek), "error" (chyba Fio, viz fio_last_error) nebo "disabled" (žádný token).
        """
        self.ensure_one()
        bank = self.sudo()
        if not bank._fio_tokens():
            return "disabled"
        if not self._fio_try_lock():
            return "wait"
        token = bank._fio_pick_token()
        if not token:
            return "wait"
        if not token.bookmark_set:
            since = fields.Date.context_today(self) - timedelta(days=LAST_INIT_DAYS)
            if self._fio_call(fio_api.set_last_date, since, token=token) is None:
                return "error"
            token.bookmark_set = True
            _logger.info("Fio %s (%s): zarážka `last` nastavena na %s", self.display_name, token._display(), since)
            return "bookmark"
        items = self._fio_call(fio_api.fetch_last, token=token)
        if items is None:
            return "error"
        created = self._fio_store_transactions(items)
        bank.fio_last_sync_at = fields.Datetime.now()
        created._match_new()
        return "synced"

    def fio_sync_recent(self):
        """Stáhne nové pohyby (`last`) dalším tokenem a spáruje je. Vrací True, když se stahovalo.

        Respektuje limit tokenů a rozestup mezi dotazy; když teď volat nelze nebo účet právě
        dotazuje jiný požadavek, nic nestahuje a vrátí False (párování dál běží nad už
        uloženými pohyby). Token bez zarážky ji napřed nastaví (jeden dotaz).
        """
        self.ensure_one()
        return self._fio_sync_recent() == "synced"

    def action_fio_fetch_now(self):
        """Tlačítko: stáhnout nové pohyby hned a spárovat vše čekající."""
        for bank in self:
            if not bank.fio_enabled:
                raise UserError(_("Účet %s nemá žádný aktivní Fio API token.", bank.display_name))
            wait = bank._fio_seconds_until_allowed()
            if wait:
                raise UserError(_("Fio teď další dotaz nedovolí, zkus to znovu za %s s.", wait))
            status = bank._fio_sync_recent()
            if status == "bookmark":
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "type": "info",
                        "title": _("Zarážka nastavena"),
                        "message": _(
                            "Token bude posílat pohyby od %(since)s. Za %(wait)s s klikni znovu na „Načíst nové pohyby“.",
                            since=fields.Date.context_today(self) - timedelta(days=LAST_INIT_DAYS),
                            wait=bank._fio_seconds_until_allowed() or bank._fio_interval_seconds(),
                        ),
                        "sticky": False,
                    },
                }
            if status != "synced":
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
            token = bank._fio_history_token()
            if not token:
                raise UserError(_("Účet %s nemá žádný aktivní Fio API token.", bank.display_name))
            if not bank.fio_history_from:
                raise UserError(_("Vyplň „Historie od“."))
            wait = token._seconds_until_allowed()
            if wait:
                raise UserError(_("Token %(token)s smí na Fio znovu za %(wait)s s.", token=token._display(), wait=wait))
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

        Historie jde vždy prvním tokenem v pořadí (jen u něj platí odemčení zámkem
        v bankovnictví), tedy nejvýš jeden dotaz za 30 s; cron se proto po každém kroku
        znovu naplánuje za 35 s, dokud je co dělat. Zarážky `last` ostatních tokenů se
        nemění: co jejich `last` stáhne znovu, se podle Fio ID přeskočí.
        """
        again = False
        today = fields.Date.context_today(self)
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
            token = bank._fio_history_token()
            if not token:
                bank.fio_history_running = False
                continue
            items = bank._fio_call(fio_api.fetch_periods, start, end, token=token)
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
        """Konec importu historie."""
        self.ensure_one()
        bank = self.sudo()
        bank.fio_history_running = False
        bank.fio_last_sync_at = fields.Datetime.now()
        _logger.info("Fio %s: import historie dokončen", bank.display_name)

    # ------------------------------------------------------------------
    # Cron každých 5 minut
    # ------------------------------------------------------------------
    @api.model
    def _cron_fio_sync(self):
        """Stáhne nové pohyby a spáruje čekající platby.

        Fio se ptá při každém běhu (5 minut), jen když některá pokladna běží – hlásí se z lišty
        každou minutu, viz `pos.config._fio_pos_active`. Uspaný nebo vypnutý počítač s pokladnou
        se nehlásí a stahuje se pak nejvýš jednou za hodinu.
        """
        now = fields.Datetime.now()
        pos_active = self.env["pos.config"]._fio_pos_active()
        for bank in self.sudo().search([("fio_enabled", "=", True), ("fio_history_running", "=", False)]):
            idle_due = not bank.fio_last_sync_at or bank.fio_last_sync_at <= now - timedelta(minutes=IDLE_SYNC_MINUTES)
            if not (pos_active or idle_due):
                continue
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
