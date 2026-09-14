# -*- coding: utf-8 -*-
"""Cron stahování z Fio z hodiny na 5 minut (cron je noupdate, aktualizace modulu ho sama nezmění)."""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE ir_cron
           SET interval_number = 5, interval_type = 'minutes'
         WHERE id IN (SELECT res_id FROM ir_model_data
                       WHERE module = 'fio_qr_payment_validation' AND name = 'cron_fio_sync')
        """
    )
