# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models

# Pokladna se hlásí každou minutu (lišta); do této doby od posledního ohlášení se bere jako běžící.
POS_ACTIVE_MINUTES = 10
# Heartbeat se zapisuje nejvýš takto často, aby se řádek pokladny nepřepisoval při každém dotazu.
HEARTBEAT_MIN_SECONDS = 30


class PosConfig(models.Model):
    _inherit = "pos.config"

    fio_pending_days = fields.Integer(
        string="Nespárované QR platby: dny zpět",
        default=14,
        help="Ikona v liště pokladny počítá QR platby čekající na banku z objednávek této pokladny "
        "za posledních N dní (0 = bez omezení).",
    )
    fio_pos_last_seen = fields.Datetime(
        string="Pokladna naposledy běžela", readonly=True, copy=False,
        help="Poslední dotaz otevřené pokladny na nespárované QR platby. Dokud je čerstvý, cron se "
        "ptá Fio každých 5 minut; uspaný nebo vypnutý počítač s pokladnou se nehlásí a stahuje se "
        "jen jednou za hodinu.",
    )

    def _fio_heartbeat(self):
        """Zapíše, že pokladna právě běží (volá se z lišty pokladny každou minutu)."""
        now = fields.Datetime.now()
        for config in self.sudo():
            last = config.fio_pos_last_seen
            if not last or (now - last).total_seconds() >= HEARTBEAT_MIN_SECONDS:
                config.fio_pos_last_seen = now

    @api.model
    def _fio_pos_active(self, minutes=POS_ACTIVE_MINUTES):
        """Běží teď některá pokladna (ohlásila se v posledních N minutách)?"""
        since = fields.Datetime.now() - timedelta(minutes=minutes)
        return bool(self.sudo().search_count([("fio_pos_last_seen", ">=", since)]))
