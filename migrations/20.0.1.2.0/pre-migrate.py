# -*- coding: utf-8 -*-
"""Odoo 19 → 20 (19.0.1.x → 20.0.1.2.0), před načtením dat modulu.

1. Práva: Odoo 20 nahradilo `ir.model.access` a `ir.rule` modelem `ir.access`. Modul má
   v `security/ir.access.csv` stejná XML ID jako dřív v `ir.model.access.csv`; kdyby v
   `ir_model_data` zůstala navázaná na starý model, načtení CSV spadne („found record of
   different model ir.model.access“). Stará XML ID modulu se proto smažou (i se starými
   řádky práv, pokud tabulka ještě existuje) a modul je založí znovu jako `ir.access`.
   Když už je převedl migrační rámec (XML ID ukazuje na `ir.access`), nic se neděje.
2. Pojistka pro skripty `point_of_sale`: typ platební metody „bankovní aplikace (QR kód)“
   se v Odoo 20 jmenuje `bank_qr_code` místo `qr_code`. Bez převodu pokladna QR kód
   nezobrazí a modul by nic neověřoval. Když hodnotu převedly už migrační skripty jádra,
   UPDATE nic nezmění.

Jen SQL, bez ORM a bez openupgradelib (běží i při obyčejném `-u` mimo OpenUpgrade).
"""
import logging

_logger = logging.getLogger(__name__)

MODULE = "fio_qr_payment_validation"
LEGACY_ACCESS_MODELS = {"ir.model.access": "ir_model_access", "ir.rule": "ir_rule"}


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return bool(cr.fetchone()[0])


def _drop_legacy_access_xmlids(cr):
    for model, table in LEGACY_ACCESS_MODELS.items():
        cr.execute(
            "SELECT id, res_id FROM ir_model_data WHERE module = %s AND model = %s",
            (MODULE, model),
        )
        rows = cr.fetchall()
        if not rows:
            continue
        if _table_exists(cr, table):
            cr.execute(
                f"DELETE FROM {table} WHERE id IN %s",  # noqa: S608 – název tabulky je konstanta
                (tuple(res_id for _id, res_id in rows),),
            )
        cr.execute("DELETE FROM ir_model_data WHERE id IN %s", (tuple(row_id for row_id, _res in rows),))
        _logger.info("%s: %s starých XML ID modelu %s smazáno, práva založí ir.access.csv", MODULE, len(rows), model)


def _rename_qr_payment_method_type(cr):
    cr.execute(
        "UPDATE pos_payment_method SET payment_method_type = 'bank_qr_code' WHERE payment_method_type = 'qr_code'"
    )
    if cr.rowcount:
        _logger.info("%s: %s platebních metod qr_code → bank_qr_code", MODULE, cr.rowcount)


def migrate(cr, version):
    if not version:
        return
    _drop_legacy_access_xmlids(cr)
    _rename_qr_payment_method_type(cr)
