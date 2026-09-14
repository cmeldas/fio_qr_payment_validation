# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    pos_fio_pending_days = fields.Integer(
        related="pos_config_id.fio_pending_days", readonly=False, string="Nespárované QR platby: dny zpět",
    )
