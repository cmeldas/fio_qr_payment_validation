# -*- coding: utf-8 -*-
"""Instalační hook: dosavadní QR platby pokladny označit jako čekající na banku."""
import logging
from datetime import timedelta

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    cr = env.cr
    cr.execute(
        """
        UPDATE pos_payment p
           SET fio_state = 'pending'
          FROM pos_payment_method m
         WHERE m.id = p.payment_method_id
           AND (m.payment_method_type = 'qr_code' OR m.qr_code_method IS NOT NULL)
           AND COALESCE(p.is_change, false) = false
           AND (p.fio_state IS NULL OR p.fio_state = 'none')
        """
    )
    _logger.info("fio_qr_payment_validation: %s QR plateb pokladny označeno jako čekající na banku", cr.rowcount)

    # výchozí začátek historie: nejstarší QR platba (s rezervou), pro účty z deníků QR metod
    cr.execute(
        """
        SELECT j.bank_account_id, min(p.payment_date)::date
          FROM pos_payment p
          JOIN pos_payment_method m ON m.id = p.payment_method_id
          JOIN account_journal j ON j.id = m.journal_id
         WHERE (m.payment_method_type = 'qr_code' OR m.qr_code_method IS NOT NULL)
           AND j.bank_account_id IS NOT NULL
         GROUP BY j.bank_account_id
        """
    )
    for bank_id, first_date in cr.fetchall():
        bank = env["res.partner.bank"].browse(bank_id)
        if bank.exists() and not bank.fio_history_from and first_date:
            bank.fio_history_from = first_date - timedelta(days=3)
            _logger.info("fio_qr_payment_validation: účet %s, historie od %s", bank.acc_number, bank.fio_history_from)
