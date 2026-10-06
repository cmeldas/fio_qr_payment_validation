# -*- coding: utf-8 -*-
"""19.0.1.1.0 → 19.0.1.2.0: token byl pole na bankovním účtu, teď je to seznam tokenů (fio.api.token).

Před aktualizací se hodnota tokenu a stav zarážky odloží do pomocné tabulky; post-migrate z ní
vytvoří první token v seznamu. Staré sloupce pak Odoo smaže samo (pole v modelu už nejsou).
"""


def migrate(cr, version):
    cr.execute(
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'res_partner_bank' AND column_name IN ('fio_api_token', 'fio_bookmark_set')
        """
    )
    columns = {row[0] for row in cr.fetchall()}
    if "fio_api_token" not in columns:
        return
    bookmark = "COALESCE(fio_bookmark_set, false)" if "fio_bookmark_set" in columns else "false"
    cr.execute("DROP TABLE IF EXISTS fio_token_migration")
    cr.execute(
        f"""
        CREATE TABLE fio_token_migration AS
        SELECT id AS bank_id, fio_api_token AS token, {bookmark} AS bookmark_set
          FROM res_partner_bank
         WHERE fio_api_token IS NOT NULL AND fio_api_token <> ''
        """
    )
