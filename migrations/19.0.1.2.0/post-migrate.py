# -*- coding: utf-8 -*-
"""19.0.1.1.0 → 19.0.1.2.0: odložený token z bankovního účtu jako první záznam v seznamu tokenů."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("SELECT to_regclass('fio_token_migration')")
    if not cr.fetchone()[0]:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Token = env["fio.api.token"].with_context(active_test=False)
    cr.execute("SELECT bank_id, token, bookmark_set FROM fio_token_migration")
    for bank_id, token, bookmark_set in cr.fetchall():
        if Token.search_count([("bank_account_id", "=", bank_id), ("token", "=", token)]):
            continue
        Token.create({"bank_account_id": bank_id, "name": "Token 1", "token": token, "bookmark_set": bool(bookmark_set)})
        _logger.info("fio_qr_payment_validation: token bankovního účtu %s převeden do seznamu tokenů", bank_id)
    cr.execute("DROP TABLE fio_token_migration")
