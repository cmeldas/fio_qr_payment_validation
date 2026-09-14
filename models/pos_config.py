# -*- coding: utf-8 -*-
from odoo import fields, models


class PosConfig(models.Model):
    _inherit = "pos.config"

    fio_pending_days = fields.Integer(
        string="Nespárované QR platby: dny zpět",
        default=14,
        help="Ikona v liště pokladny počítá QR platby čekající na banku z objednávek této pokladny "
        "za posledních N dní (0 = bez omezení).",
    )
