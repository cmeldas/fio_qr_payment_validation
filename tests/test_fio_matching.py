# -*- coding: utf-8 -*-
"""Párování pohybů Fio s QR platbami pokladny a fakturami. Fio API se nevolá, odpovědi se podstrkují."""
import ast
from datetime import date, timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tools import mute_logger
from psycopg2 import IntegrityError
from odoo.tests import tagged
from odoo.addons.point_of_sale.tests.common import CommonPosTest

from ..models import fio_api


def fio_cell(value):
    return None if value is None else {"value": value}


def fio_transaction(fio_id, day, amount, message=None, vs=None, ident=None, counter_name="Jan Novák", currency="CZK"):
    """Jeden pohyb ve tvaru, v jakém ho posílá Fio (sloupce column<N>)."""
    return {
        "column22": fio_cell(fio_id),
        "column0": fio_cell(f"{day:%Y-%m-%d}+0200"),
        "column1": fio_cell(amount),
        "column14": fio_cell(currency),
        "column2": fio_cell("123456789"),
        "column3": fio_cell("0800"),
        "column10": fio_cell(counter_name),
        "column4": None,
        "column5": fio_cell(vs),
        "column6": None,
        "column7": fio_cell(ident),
        "column16": fio_cell(message),
        "column8": fio_cell("Okamžitá příchozí platba"),
        "column9": None,
        "column18": None,
        "column25": None,
        "column26": None,
        "column17": None,
    }


def fio_payload(transactions):
    return {
        "accountStatement": {
            "info": {"accountId": "2401835999", "bankId": "2010", "currency": "CZK"},
            "transactionList": {"transaction": transactions},
        }
    }


@tagged("post_install", "-at_install")
class TestFioMatching(CommonPosTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Holé Odoo nemá žádnou metodu QR kódu (dodává ji např. cz_qr_payment); pro platební
        # metodu typu QR se podstrčí testovací.
        cls.startClassPatcher(patch.object(
            type(cls.env["res.partner.bank"]), "_get_available_qr_methods",
            return_value=[("test_qr", "Testovací QR", 10)],
        ))
        cls.bank_account = cls.env["res.partner.bank"].create({
            "acc_number": "CZ3820100000002401835999",
            "partner_id": cls.env.company.partner_id.id,
            # jako po prvním kontaktu s Fio (zarážka nastavená); samotné nastavení testuje test_first_sync_sets_bookmark
            "fio_token_ids": [Command.create({"name": "A", "token": "a" * 64, "bookmark_set": True})],
        })
        cls.bank_journal = cls.env["account.journal"].create({
            "name": "Fio", "type": "bank", "code": "FIO",
            "company_id": cls.env.company.id,
            "bank_account_id": cls.bank_account.id,
        })
        cls.qr_payment_method = cls.env["pos.payment.method"].create({
            "name": "QR", "journal_id": cls.bank_journal.id, "payment_method_type": "qr_code",
            "qr_code_method": "test_qr",
            "receivable_account_id": cls.company_data["default_account_receivable"].id,
        })
        cls.pos_config_usd.write({"payment_method_ids": [Command.link(cls.qr_payment_method.id)]})
        cls.product_80 = cls.env["product.product"].create({
            "name": "Zmrzlina", "type": "consu", "list_price": 80.0, "available_in_pos": True, "taxes_id": [Command.clear()],
        })
        cls.product_77 = cls.env["product.product"].create({
            "name": "Káva", "type": "consu", "list_price": 77.0, "available_in_pos": True, "taxes_id": [Command.clear()],
        })
        cls.currency_id = cls.env.company.currency_id.id

    def _rest(self, bank=None):
        """Jako by od posledních dotazů uplynulo dost času: účet i všechny jeho tokeny volné."""
        bank = bank or self.bank_account
        bank.fio_last_request_at = False
        bank.with_context(active_test=False).fio_token_ids.write({"last_request_at": False})

    def _add_tokens(self, *names):
        """Další tokeny k účtu (se zarážkou), vrací nové záznamy."""
        return self.env["fio.api.token"].create([
            {"bank_account_id": self.bank_account.id, "name": name, "token": name.lower() * 64, "bookmark_set": True,
             "sequence": 20 + index}
            for index, name in enumerate(names)
        ])

    def _store(self, transactions):
        items = fio_api.parse_transactions(fio_payload(transactions))
        return self.bank_account._fio_store_transactions(items)

    def _order(self, product, payment_method, pos_reference=None):
        order_data = {"pos_reference": pos_reference} if pos_reference else {}
        order, _refund = self.create_backend_pos_order({
            "line_data": [{"product_id": product.id}],
            "payment_data": [{"payment_method_id": payment_method.id}],
            "order_data": order_data,
        })
        return order

    # ------------------------------------------------------------------
    def test_parse_transactions(self):
        items = fio_api.parse_transactions(fio_payload([
            fio_transaction(1, date(2026, 9, 12), 80.0, "Order 261-1-000005 5", vs="123"),
            fio_transaction(2, date(2026, 9, 12), -50.0),
        ]))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["fio_id"], "1")
        self.assertEqual(items[0]["date"], date(2026, 9, 12))
        self.assertEqual(items[0]["amount"], 80.0)
        self.assertEqual(items[0]["variable_symbol"], "123")
        self.assertEqual(items[0]["counter_name"], "Jan Novák")
        self.assertEqual(items[0]["message"], "Order 261-1-000005 5")
        self.assertEqual(items[1]["amount"], -50.0)
        self.assertIsNone(items[1]["message"])
        with self.assertRaises(fio_api.FioApiError):
            fio_api.parse_transactions({"foo": 1})

    def test_reference_keys_and_digit_boundary(self):
        Transaction = self.env["fio.bank.transaction"]
        self.assertEqual(Transaction._pos_reference_keys("Order 00275-006-0114"), {"order00275-006-0114", "00275-006-0114"})
        self.assertEqual(Transaction._pos_reference_keys("Objednávka 00275-006-0114"), {"objednavka00275-006-0114", "00275-006-0114"})
        self.assertEqual(Transaction._pos_reference_keys("261-1-000005"), {"261-1-000005"})
        self.assertEqual(Transaction._pos_reference_keys(False), set())
        tx = self._store([fio_transaction(10, date.today(), 1.0, "Order 261-1-000005 7")])
        self.assertTrue(tx._text_matches_reference("261-1-000005"))
        self.assertTrue(tx._text_matches_reference("Objednávka 261-1-000005"))
        self.assertFalse(tx._text_matches_reference("61-1-000005"), "kratší číslo nesmí sedět na konec delšího")
        self.assertFalse(tx._text_matches_reference("261-1-000006"))
        tx_ident = self._store([fio_transaction(11, date.today(), 1.0, None, ident="OBJEDNÁVKA 00275-006-0114")])
        self.assertTrue(tx_ident._text_matches_reference("Order 00275-006-0114"))

    def test_qr_payment_pending_then_matched(self):
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000005")
        payment = order.payment_ids
        self.assertEqual(payment.fio_state, "pending")
        cash_order = self._order(self.product_80, self.cash_payment_method)
        self.assertEqual(cash_order.payment_ids.fio_state, "none")

        txs = self._store([
            fio_transaction(100, date.today(), order.amount_total - 1, "Objednávka 261-1-000005 5"),  # nižší → ne
            fio_transaction(101, date.today(), order.amount_total + 20, "Objednávka 261-1-000005 5"),  # dýško 20
            fio_transaction(102, date.today(), order.amount_total, "Order 999-9-999999 5"),
            fio_transaction(103, date.today(), -12.0, "odchozí"),
        ])
        self.assertEqual(len(txs), 4)
        txs._match_new()
        self.assertEqual(txs[0].state, "new", "pohyb nižší než platba se nepáruje")
        self.assertEqual(payment.fio_state, "confirmed")
        self.assertEqual(payment.fio_transaction_id, txs[1])
        self.assertEqual(txs[1].match_type, "reference")
        self.assertAlmostEqual(txs[1].tip_amount, 20.0)
        self.assertAlmostEqual(payment.fio_tip_amount, 20.0)
        self.assertEqual(txs[2].state, "new")
        self.assertEqual(txs[3].state, "new")
        self.assertFalse(txs[3].is_incoming)
        self.assertFalse(self._store([fio_transaction(101, date.today(), order.amount_total, "duplicita")]))

        txs[1].action_reset()
        self.assertEqual(txs[1].state, "new")
        self.assertEqual(txs[1].tip_amount, 0.0)
        self.assertEqual(payment.fio_state, "pending")
        self.assertFalse(payment.fio_transaction_id)

        payment.action_fio_confirm_manual()
        self.assertEqual(payment.fio_state, "manual")
        self.env["fio.bank.transaction"]._match_pending()
        self.assertEqual(payment.fio_state, "confirmed", "ručně potvrzenou platbu cron dopáruje")
        self.assertEqual(payment.fio_transaction_id, txs[1])
        self.assertEqual(txs[0].state, "new")

    def test_pos_client_does_not_load_fio_fields(self):
        """Pokladna Odoo 19 posílá při uložení všechna načtená pole platby; stav ověření musí zůstat jen na serveru."""
        fields_list = self.env["pos.payment"]._load_pos_data_fields(self.pos_config_usd)
        self.assertIn("amount", fields_list)
        self.assertIn("payment_method_id", fields_list)
        self.assertIn("pos_order_id", fields_list)
        for name in ("fio_state", "fio_transaction_id", "fio_confirmed_at", "fio_match_type", "fio_tip_amount", "pos_reference"):
            self.assertNotIn(name, fields_list)

    def test_create_with_empty_fio_state_from_client(self):
        """Objednávka uložená pokladnou z doby před aktualizací pošle fio_state=False – nesmí spadnout."""
        order = self._order(self.product_80, self.cash_payment_method)
        Payment = self.env["pos.payment"]
        qr = Payment.create({
            "pos_order_id": order.id, "session_id": order.session_id.id,
            "payment_method_id": self.qr_payment_method.id, "amount": 1.0, "fio_state": False,
        })
        self.assertEqual(qr.fio_state, "pending")
        cash = Payment.create({
            "pos_order_id": order.id, "session_id": order.session_id.id,
            "payment_method_id": self.cash_payment_method.id, "amount": 1.0, "fio_state": False,
        })
        self.assertEqual(cash.fio_state, "none")
        qr.write({"fio_state": False, "amount": 2.0})
        self.assertEqual(qr.fio_state, "pending")
        self.assertEqual(qr.amount, 2.0)

    def test_pending_overview_for_pos_navbar(self):
        Payment = self.env["pos.payment"]
        config = self.pos_config_usd
        before = Payment.fio_pending_overview(config.id)
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000088")
        payment = order.payment_ids
        cash_order = self._order(self.product_80, self.cash_payment_method)
        overview = Payment.fio_pending_overview(config.id)
        self.assertEqual(overview["count"], before["count"] + 1)
        row = next(r for r in overview["payments"] if r["id"] == payment.id)
        self.assertEqual(row["pos_reference"], "Order 261-1-000088")
        self.assertEqual(row["order_id"], order.id)
        self.assertAlmostEqual(row["amount"], order.amount_total)
        self.assertTrue(row["payment_date"])
        self.assertFalse(any(r["order_id"] == cash_order.id for r in overview["payments"]))

        payment.action_fio_confirm_manual()
        self.assertEqual(Payment.fio_pending_overview(config.id)["count"], before["count"], "ručně potvrzená zmizí")
        payment.action_fio_reset_pending()
        payment.action_fio_ignore()
        self.assertEqual(Payment.fio_pending_overview(config.id)["count"], before["count"], "ignorovaná zmizí")

        payment.action_fio_reset_pending()
        config.fio_pending_days = 1
        payment.write({"payment_date": fields.Datetime.now() - timedelta(days=3)})
        self.assertFalse(any(r["id"] == payment.id for r in Payment.fio_pending_overview(config.id)["payments"]),
                         "starší než nastavený počet dní se nepočítá")
        config.fio_pending_days = 0
        self.assertTrue(any(r["id"] == payment.id for r in Payment.fio_pending_overview(config.id)["payments"]))
        self.assertEqual(Payment.fio_pending_overview(self.pos_config_eur.id)["count"], 0, "jiná pokladna nevidí cizí platby")

    def test_tips_today_and_grouping_fields(self):
        Payment = self.env["pos.payment"]
        config = self.pos_config_usd
        base = Payment.fio_pending_overview(config.id)
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000090")
        payment = order.payment_ids
        tx = self._store([fio_transaction(701, date.today(), order.amount_total + 15, "Order 261-1-000090 9")])
        tx._match_new()
        self.assertAlmostEqual(tx.tip_amount, 15.0)
        self.assertEqual(tx.pos_payment_date, payment.payment_date, "den platby se ukládá kvůli seskupení po dnech")
        self.assertEqual(tx.pos_config_id, config)

        overview = Payment.fio_pending_overview(config.id)
        self.assertAlmostEqual(overview["tip_today"], base["tip_today"] + 15.0)
        self.assertEqual(overview["tip_today_count"], base["tip_today_count"] + 1)
        self.assertEqual(overview["tips_url"], "/odoo/action-fio_qr_payment_validation.action_fio_tips")
        self.assertEqual(Payment.fio_pending_overview(self.pos_config_eur.id)["tip_today"], 0.0)

        # dýško ze včerejší platby se do „dnes“ nepočítá
        payment.write({"payment_date": fields.Datetime.now() - timedelta(days=1)})
        self.assertAlmostEqual(Payment.fio_pending_overview(config.id)["tip_today"], base["tip_today"])

        action = self.env.ref("fio_qr_payment_validation.action_fio_tips")
        tips = self.env["fio.bank.transaction"].search(ast.literal_eval(action.domain))
        self.assertIn(tx, tips)
        tx.action_reset()
        self.assertEqual(tx.tip_amount, 0.0)
        self.assertFalse(tx.pos_payment_date)

    def test_ignored_payment_matched_by_reference_when_money_arrives_later(self):
        """Obsluha platbu ignorovala (peníze nepřišly), zákazník zaplatil o dny později: spáruje se podle čísla objednávky."""
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="2611-1-000333")
        payment = order.payment_ids
        payment.action_fio_ignore()
        self.assertEqual(payment.fio_state, "ignored")
        # QR zpráva bez mezer: Order + číslo objednávky + tracking
        tx = self._store([fio_transaction(950, date.today() + timedelta(days=3), order.amount_total, "Order2611-1-00033311333",
                                          ident="Theodor Petřík")])
        tx._match_new()
        self.assertEqual(tx.state, "matched")
        self.assertEqual(tx.match_type, "reference")
        self.assertEqual(tx.pos_payment_id, payment)
        self.assertEqual(payment.fio_state, "confirmed")
        self.assertTrue(tx.note, "u pohybu je poznámka, že platba byla ignorovaná")

    def test_ignored_payment_not_matched_by_amount_only(self):
        """Bez čísla objednávky ve zprávě se ignorovaná platba nepáruje (ani cronem, ani ručním průvodcem předvyplněním)."""
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000444")
        payment = order.payment_ids
        payment.action_fio_ignore()
        tx = self._store([fio_transaction(951, date.today(), order.amount_total, "bez cisla objednavky")])
        tx._match_new()
        self.env["fio.bank.transaction"]._match_pending()
        self.assertEqual(tx.state, "new")
        self.assertEqual(payment.fio_state, "ignored")

    def test_pending_payment_preferred_over_ignored(self):
        older = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000555")
        newer = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000555")
        newer.payment_ids.action_fio_ignore()
        tx = self._store([fio_transaction(952, date.today(), older.amount_total, "Order 261-1-000555 5")])
        tx._match_new()
        self.assertEqual(tx.pos_payment_id, older.payment_ids, "čekající platba má přednost před ignorovanou, i když je starší")
        self.assertEqual(newer.payment_ids.fio_state, "ignored")

    def test_exact_amount_without_tip(self):
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000006")
        tx = self._store([fio_transaction(110, date.today(), order.amount_total, "Order 261-1-000006 6")])
        tx._match_new()
        self.assertEqual(order.payment_ids.fio_state, "confirmed")
        self.assertEqual(tx.tip_amount, 0.0)

    def test_pos_check_reserves_transaction(self):
        method = self.qr_payment_method
        items = fio_api.parse_transactions(fio_payload([fio_transaction(201, date.today(), 77.0, "bez cisla")]))
        with patch.object(fio_api, "fetch_last", return_value=items) as fetch_last:
            res = method.fio_check_qr_payment(77.0, "Order 300-1-000001", self.currency_id)
            self.assertTrue(res["enabled"])
            self.assertTrue(res["confirmed"])
            self.assertEqual(res["match_type"], "amount")
            fetch_last.assert_called_once()
            res2 = method.fio_check_qr_payment(77.0, "Order 300-1-000001", self.currency_id)
            self.assertTrue(res2["confirmed"])
            fetch_last.assert_called_once()  # do 30 s se Fio znovu nevolá
            res3 = method.fio_check_qr_payment(77.0, "Order 300-1-000002", self.currency_id)
            self.assertFalse(res3["confirmed"], "rezervovaný pohyb nedostane jiná objednávka")
            self.assertGreater(res3["next_check_in"], 0)
            self.assertFalse(res3["synced"], "do 30 s se Fio nevolá, pokladna jen odpočítává")
            self.assertEqual(res3["interval"], 32, "jeden token = dotaz každých 32 s (30 s limit + rezerva)")
            self.assertEqual(res3["token_count"], 1)
        self._rest()
        with patch.object(fio_api, "fetch_last", return_value=[]):
            res4 = method.fio_check_qr_payment(77.0, "Order 300-1-000002", self.currency_id)
        self.assertTrue(res4["synced"], "po uplynutí limitu se stáhne z banky a ukazatel začne od plna")
        self.assertGreaterEqual(res4["next_check_in"], 30)
        tx = self.env["fio.bank.transaction"].browse(res["transaction_id"])
        self.assertEqual(tx.reserved_reference, "Order 300-1-000001")
        self.assertEqual(tx.state, "new")

        order = self._order(self.product_77, method, pos_reference="Order 300-1-000001")
        payment = order.payment_ids
        self.assertEqual(payment.fio_state, "confirmed", "uložení objednávky spáruje rezervovaný pohyb")
        self.assertEqual(payment.fio_transaction_id, tx)
        self.assertEqual(tx.match_type, "amount")
        self.assertFalse(tx.reserved_reference)

    def test_pos_check_tip_rules(self):
        method = self.qr_payment_method
        # jen podle částky: dýško do 50 % platby projde, větší pohyb bez čísla objednávky ne
        items = fio_api.parse_transactions(fio_payload([fio_transaction(211, date.today(), 90.0, "bez cisla")]))
        with patch.object(fio_api, "fetch_last", return_value=items):
            res = method.fio_check_qr_payment(77.0, "Order 300-1-000011", self.currency_id)
        self.assertTrue(res["confirmed"])
        self.assertEqual(res["match_type"], "amount")
        self.assertAlmostEqual(res["tip"], 13.0)

        self._rest()
        items = fio_api.parse_transactions(fio_payload([fio_transaction(212, date.today(), 200.0, "bez cisla")]))
        with patch.object(fio_api, "fetch_last", return_value=items):
            res = method.fio_check_qr_payment(77.0, "Order 300-1-000012", self.currency_id)
        self.assertFalse(res["confirmed"], "pohyb o víc než 50 % vyšší bez čísla objednávky se nebere")

        # s číslem objednávky projde libovolně vyšší pohyb, nižší nikdy
        self._rest()
        items = fio_api.parse_transactions(fio_payload([
            fio_transaction(213, date.today(), 70.0, "Order 300-1-000013 1"),
            fio_transaction(214, date.today(), 200.0, "Order 300-1-000013 1"),
        ]))
        with patch.object(fio_api, "fetch_last", return_value=items):
            res = method.fio_check_qr_payment(77.0, "Order 300-1-000013", self.currency_id)
        self.assertTrue(res["confirmed"])
        self.assertEqual(res["match_type"], "reference")
        self.assertAlmostEqual(res["amount"], 200.0)
        self.assertAlmostEqual(res["tip"], 123.0)

    def test_pos_check_disabled_without_token(self):
        self.bank_account.fio_token_ids.active = False
        self.assertFalse(self.bank_account.fio_enabled, "bez aktivního tokenu je párování vypnuté")
        self.assertEqual(
            self.qr_payment_method.fio_check_qr_payment(1.0, "Order 1-1-000001", self.currency_id),
            {"enabled": False},
        )

    def test_first_sync_sets_bookmark(self):
        bank = self.bank_account
        bank.fio_token_ids.bookmark_set = False
        with patch.object(fio_api, "set_last_date") as set_last_date, patch.object(fio_api, "fetch_last") as fetch_last:
            self.assertFalse(bank.fio_sync_recent(), "první volání jen nastaví zarážku")
            set_last_date.assert_called_once()
            self.assertEqual(set_last_date.call_args.args[1], date.today() - timedelta(days=85))
            fetch_last.assert_not_called()
        self.assertTrue(bank.fio_token_ids.bookmark_set)
        self._rest(bank)
        with patch.object(fio_api, "fetch_last", return_value=[]) as fetch_last:
            self.assertTrue(bank.fio_sync_recent())
            fetch_last.assert_called_once()

    def test_rate_limit_and_errors(self):
        bank = self.bank_account
        bank.fio_last_request_at = fields.Datetime.now()
        self.assertGreater(bank._fio_seconds_until_allowed(), 0)
        with patch.object(fio_api, "fetch_last") as fetch_last:
            self.assertFalse(bank.fio_sync_recent())
            fetch_last.assert_not_called()
        bank.fio_last_request_at = fields.Datetime.now() - timedelta(seconds=31)
        self.assertGreater(bank._fio_seconds_until_allowed(), 0, "rezerva 2 s: po 31 s ještě ne")
        bank.fio_last_request_at = fields.Datetime.now() - timedelta(seconds=33)
        self.assertEqual(bank._fio_seconds_until_allowed(), 0)
        with patch.object(fio_api, "fetch_last", side_effect=fio_api.FioRateLimitError("HTTP 409")):
            self.assertFalse(bank.fio_sync_recent())
        self.assertIn("409", bank.fio_last_error)
        self.assertIn("409", bank.fio_token_ids.last_error, "chyba se uloží i k tokenu")
        self._rest(bank)
        with patch.object(fio_api, "fetch_last", return_value=[]):
            self.assertTrue(bank.fio_sync_recent())
        self.assertFalse(bank.fio_last_error)
        with self.assertRaises(UserError):
            bank.action_fio_fetch_now()  # hned po dotazu: limit 30 s

    def test_invoice_matched_by_variable_symbol(self):
        partner = self.env["res.partner"].create({"name": "Zákazník s fakturou"})
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id, "invoice_date": date.today(),
            "invoice_line_ids": [Command.create({"name": "Zmrzlina na akci", "quantity": 1, "price_unit": 250.0})],
        })
        invoice.action_post()
        vs = "".join(ch for ch in invoice.payment_reference if ch.isdigit())
        self.assertTrue(vs)
        currency = invoice.currency_id.name  # testovací firma neúčtuje v CZK; jiná měna by platbu nezaregistrovala
        tx = self._store([fio_transaction(301, date.today(), invoice.amount_total, "platba faktury", vs=vs, currency=currency)])
        tx._match_new()
        self.assertEqual(tx.state, "matched")
        self.assertEqual(tx.match_type, "variable_symbol")
        self.assertEqual(tx.move_id, invoice)
        self.assertEqual(tx.payment_id.journal_id, self.bank_journal)
        self.assertEqual(tx.payment_id.amount, invoice.amount_total)
        self.assertEqual(tx.payment_id.fio_transaction_id, tx)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.assertEqual(invoice.fio_transaction_count, 1)

        tx2 = self._store([fio_transaction(302, date.today(), invoice.amount_total, "faktura znovu", vs=vs, currency=currency)])
        tx2._match_new()
        self.assertEqual(tx2.state, "matched", "už uhrazená faktura se jen propojí")
        self.assertFalse(tx2.payment_id)
        self.assertTrue(tx2.note)

        tx3 = self._store([fio_transaction(303, date.today(), 10.0, "neznámý VS", vs="999999999", currency=currency)])
        tx3._match_new()
        self.assertEqual(tx3.state, "new")

    def test_invoice_other_currency_links_without_payment(self):
        partner = self.env["res.partner"].create({"name": "Zákazník v cizí měně"})
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id, "invoice_date": date.today(),
            "invoice_line_ids": [Command.create({"name": "Služba", "quantity": 1, "price_unit": 100.0})],
        })
        invoice.action_post()
        vs = "".join(ch for ch in invoice.payment_reference if ch.isdigit())
        other = "CZK" if invoice.currency_id.name != "CZK" else "EUR"
        tx = self._store([fio_transaction(311, date.today(), invoice.amount_total, "cizí měna", vs=vs, currency=other)])
        tx._match_new()
        self.assertEqual(tx.state, "matched")
        self.assertEqual(tx.move_id, invoice)
        self.assertFalse(tx.payment_id)
        self.assertEqual(invoice.payment_state, "not_paid")

    def test_manual_match_wizard_and_ignore(self):
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000009")
        payment = order.payment_ids
        tx = self._store([fio_transaction(401, date.today(), order.amount_total, "bez odkazu")])
        tx._match_new()
        self.assertEqual(tx.state, "new", "cron bez čísla objednávky nepáruje jen podle částky")

        lower = self._store([fio_transaction(402, date.today(), order.amount_total - 5, "míň")])
        wizard_lower = self.env["fio.match.wizard"].with_context(default_transaction_id=lower.id).create({
            "pos_payment_id": payment.id,
        })
        with self.assertRaises(UserError, msg="nižší pohyb nejde spárovat ani ručně"):
            wizard_lower.action_confirm()

        wizard = self.env["fio.match.wizard"].with_context(default_transaction_id=tx.id).create({})
        wizard._onchange_transaction_id()
        self.assertEqual(wizard.pos_payment_id, payment, "jediná čekající platba ve stejné částce se předvyplní")
        wizard.action_confirm()
        self.assertEqual(tx.match_type, "manual")
        self.assertEqual(payment.fio_state, "confirmed")
        with self.assertRaises(UserError):
            tx.action_ignore()

        payment.action_fio_reset_pending()
        self.assertEqual(payment.fio_state, "pending")
        self.assertEqual(tx.state, "new")
        tx.action_ignore()
        self.assertEqual(tx.state, "ignored")
        payment.action_fio_ignore()
        self.assertEqual(payment.fio_state, "ignored")

    def test_history_import_steps(self):
        bank = self.bank_account
        Bank = self.env["res.partner.bank"]
        calls = []
        second = self._add_tokens("B")

        def fetch_periods(token, day_from, day_to):
            calls.append((day_from, day_to))
            return fio_api.parse_transactions(fio_payload([
                fio_transaction(500 + len(calls), day_from, 1.0, f"historie {len(calls)}"),
            ]))

        bank.write({"fio_history_from": date.today() - timedelta(days=800)})
        used_tokens = []

        def fetch_periods_tracked(token, day_from, day_to):
            used_tokens.append(token)
            return fetch_periods(token, day_from, day_to)

        with patch.object(fio_api, "fetch_periods", side_effect=fetch_periods_tracked):
            bank.action_fio_import_history()
            self.assertTrue(bank.fio_history_running)
            for _step in range(3):
                self._rest(bank)  # jako by uplynulo 30 s
                Bank._cron_fio_history_step()
            self.assertFalse(bank.fio_history_running)
            self.assertEqual(len(calls), 3, "800 dní = tři roční bloky")
            self.assertEqual(calls[0][0], date.today() - timedelta(days=800))
            self.assertEqual(calls[1][0], calls[0][1] + timedelta(days=1))
            self.assertEqual(calls[-1][1], date.today())
        self.assertEqual(set(used_tokens), {"a" * 64}, "historie jde jen prvním tokenem (odemčení zámkem platí pro jeden)")
        self.assertFalse(second.last_request_at)
        stored = self.env["fio.bank.transaction"].search([
            ("bank_account_id", "=", bank.id), ("fio_id", "in", ["501", "502", "503"]),
        ])
        self.assertEqual(len(stored), 3)

    def test_history_import_stops_on_api_error(self):
        bank = self.bank_account
        bank.write({"fio_history_from": date.today() - timedelta(days=10)})
        self._rest(bank)
        with patch.object(fio_api, "fetch_periods", side_effect=fio_api.FioApiError("Fio API: HTTP 422 – odmítnuto")):
            bank.action_fio_import_history()
            self.env["res.partner.bank"]._cron_fio_history_step()
        self.assertFalse(bank.fio_history_running, "po chybě jiné než limit se import nezacyklí")
        self.assertIn("422", bank.fio_last_error)
        self._rest(bank)
        with patch.object(fio_api, "fetch_periods", side_effect=fio_api.FioRateLimitError("HTTP 409")):
            bank.action_fio_import_history()
            self.env["res.partner.bank"]._cron_fio_history_step()
        self.assertTrue(bank.fio_history_running, "po limitu 409 import pokračuje dalším krokem")

    # ------------------------------------------------------------------
    # Víc tokenů: střídání, interval, zarážky
    # ------------------------------------------------------------------
    def test_interval_by_token_count(self):
        Bank = self.env["res.partner.bank"]
        self.assertEqual(
            [Bank._fio_interval_for(n) for n in (0, 1, 2, 3, 4, 7, 15, 40)],
            [32, 32, 16, 11, 8, 5, 3, 2],
            "(30 s + 2 s rezerva) / počet tokenů zaokrouhleno nahoru, nejméně 2 s",
        )
        bank = self.bank_account
        self.assertEqual((bank.fio_token_count, bank.fio_interval), (1, 32))
        b, c = self._add_tokens("B", "C")
        self.assertEqual((bank.fio_token_count, bank.fio_interval), (3, 11))
        c.active = False
        self.assertEqual((bank.fio_token_count, bank.fio_interval), (2, 16), "neaktivní token se nepočítá")
        with patch.object(fio_api, "fetch_last", return_value=[]):
            res = self.qr_payment_method.fio_check_qr_payment(1.0, "Order 1-1-000001", self.currency_id)
        self.assertEqual(res["interval"], 16)
        self.assertEqual(res["token_count"], 2)
        self.assertEqual(self.env["pos.payment"].fio_pending_overview(self.pos_config_usd.id)["interval"], 16)

    def test_tokens_take_turns(self):
        """Tokeny se volají postupně za sebou, vždy ten nejdéle nepoužitý; každý nejvýš jednou za 30 s."""
        bank = self.bank_account
        self._add_tokens("B", "C")
        used = []
        now = fields.Datetime.now()

        def fetch_last(token):
            used.append(token[0])
            return []

        with patch.object(fio_api, "fetch_last", side_effect=fetch_last):
            self.assertTrue(bank.fio_sync_recent())
            self.assertFalse(bank.fio_sync_recent(), "hned za ním ne: rozestup 11 s mezi dotazy")
            self.assertGreaterEqual(bank._fio_seconds_until_allowed(), 11)
            for step in range(1, 6):
                # posun času o 11 s: rozestup splněný, tokeny se střídají a každý čeká svých 30 s
                bank.fio_last_request_at = now - timedelta(seconds=11)
                for token in bank.fio_token_ids:
                    if token.last_request_at:
                        token.last_request_at -= timedelta(seconds=11)
                self.assertTrue(bank.fio_sync_recent(), f"krok {step}")
        self.assertEqual("".join(used), "abcabc", "střídání a → b → c → a …")
        # všechny tokeny použité před méně než 32 s: další dotaz nejde, ani když uplyne rozestup
        bank.fio_last_request_at = now - timedelta(seconds=11)
        bank.fio_token_ids.write({"last_request_at": now - timedelta(seconds=20)})
        self.assertGreater(bank._fio_seconds_until_allowed(), 0)
        with patch.object(fio_api, "fetch_last") as fetch_last:
            self.assertFalse(bank.fio_sync_recent())
            fetch_last.assert_not_called()

    def test_all_tokens_cycle_with_sparse_calls_and_a_failing_token(self):
        """Řídké dotazy (cron po hodině) i nefunkční token: na řadu přijdou postupně všechny tokeny."""
        bank = self.bank_account
        b, c, d = self._add_tokens("B", "C", "D")
        used = []

        def fetch_last(token):
            used.append(token[0])
            if token[0] == "c":
                raise fio_api.FioApiError("Fio API: HTTP 500 – neexistující nebo neaktivní token")
            return []

        with patch.object(fio_api, "fetch_last", side_effect=fetch_last):
            for _hour in range(8):
                bank._fio_sync_recent()
                # o hodinu později: posunout všechny časy dozadu
                if bank.fio_last_request_at:
                    bank.fio_last_request_at -= timedelta(hours=1)
                for token in bank.fio_token_ids:
                    if token.last_request_at:
                        token.last_request_at -= timedelta(hours=1)
        self.assertEqual("".join(used), "abcdabcd", "každý aktivní token se použije, i ten poslední a i ten s chybou")
        tokens = bank.fio_token_ids
        self.assertTrue(all(tokens.mapped("last_request_at")))
        self.assertFalse(c.last_success_at, "nefunkční token nemá úspěšný dotaz")
        self.assertIn("500", c.last_error)
        self.assertTrue(all((tokens - c).mapped("last_success_at")))
        self.assertFalse((tokens - c).filtered("last_error"))

    def test_new_token_sets_its_own_bookmark(self):
        bank = self.bank_account
        bank.fio_token_ids.last_request_at = fields.Datetime.now()  # token A teď nesmí
        bank.fio_last_request_at = fields.Datetime.now() - timedelta(seconds=40)
        new = self.env["fio.api.token"].create({"bank_account_id": bank.id, "name": "Nový", "token": " n" * 32})
        self.assertEqual(new.token, "n" * 32, "mezery z vloženého tokenu se odstraní")
        self.assertFalse(new.bookmark_set)
        with patch.object(fio_api, "set_last_date") as set_last_date, patch.object(fio_api, "fetch_last") as fetch_last:
            self.assertEqual(bank._fio_sync_recent(), "bookmark")
            set_last_date.assert_called_once()
            self.assertEqual(set_last_date.call_args.args[0], "n" * 32, "zarážku si nastaví právě nový token")
            fetch_last.assert_not_called()
        self.assertTrue(new.bookmark_set)
        new.token = "m" * 64
        self.assertFalse(new.bookmark_set, "jiná hodnota tokenu = nová zarážka")

    def test_stale_bookmark_422_resets_token(self):
        bank = self.bank_account
        token = bank.fio_token_ids
        with patch.object(fio_api, "fetch_last", side_effect=fio_api.FioHistoryLockedError("Fio API: HTTP 422 – data starší 90 dní")):
            self.assertEqual(bank._fio_sync_recent(), "error")
        self.assertFalse(token.bookmark_set, "zarážka starší 90 dní: příště se nastaví znovu")
        self.assertIn("422", token.last_error)
        self._rest(bank)
        with patch.object(fio_api, "set_last_date") as set_last_date:
            self.assertEqual(bank._fio_sync_recent(), "bookmark")
            set_last_date.assert_called_once()
        self.assertTrue(token.bookmark_set)
        self.assertFalse(token.last_error)

    def test_duplicate_or_short_token_refused(self):
        with self.assertRaises(ValidationError):
            self.env["fio.api.token"].create({"bank_account_id": self.bank_account.id, "token": "kratky"})
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            with self.env.cr.savepoint():
                self.env["fio.api.token"].create({"bank_account_id": self.bank_account.id, "token": "a" * 64})

    def test_cron_syncs_and_matches(self):
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000077")
        payment = order.payment_ids
        items = fio_api.parse_transactions(fio_payload([
            fio_transaction(601, date.today(), order.amount_total, "Order 261-1-000077 3"),
        ]))
        self.bank_account.fio_last_sync_at = False  # nikdy nestaženo → stahuje se i bez běžící pokladny
        with patch.object(fio_api, "fetch_last", return_value=items):
            self.env["res.partner.bank"]._cron_fio_sync()
        self.assertEqual(payment.fio_state, "confirmed")
        self.assertEqual(payment.fio_transaction_id.fio_id, "601")

    def test_cron_every_5_minutes_only_while_pos_runs(self):
        bank = self.bank_account
        Bank = self.env["res.partner.bank"]
        self.env["pos.config"].search([]).write({"fio_pos_last_seen": False})
        bank.write({"fio_last_sync_at": fields.Datetime.now() - timedelta(minutes=10)})
        self._rest(bank)
        with patch.object(fio_api, "fetch_last", return_value=[]) as fetch_last:
            Bank._cron_fio_sync()
            fetch_last.assert_not_called()  # žádná pokladna neběží a poslední stažení je čerstvé
        bank.write({"fio_last_sync_at": fields.Datetime.now() - timedelta(minutes=61)})
        self._rest(bank)
        with patch.object(fio_api, "fetch_last", return_value=[]) as fetch_last:
            Bank._cron_fio_sync()
            fetch_last.assert_called_once()  # po hodině se stáhne i bez pokladny
        bank.write({"fio_last_sync_at": fields.Datetime.now()})
        self._rest(bank)
        self.env["pos.payment"].fio_pending_overview(self.pos_config_usd.id)  # lišta pokladny se ohlásila
        self.assertTrue(self.pos_config_usd.fio_pos_last_seen)
        self.assertTrue(self.env["pos.config"]._fio_pos_active())
        with patch.object(fio_api, "fetch_last", return_value=[]) as fetch_last:
            Bank._cron_fio_sync()
            fetch_last.assert_called_once()  # pokladna běží → stahuje se každých 5 minut
        self.pos_config_usd.fio_pos_last_seen = fields.Datetime.now() - timedelta(minutes=15)
        self.assertFalse(self.env["pos.config"]._fio_pos_active(), "po 10 minutách bez ohlášení se pokladna bere jako uspaná")
        cron = self.env.ref("fio_qr_payment_validation.cron_fio_sync")
        self.assertEqual((cron.interval_number, cron.interval_type), (5, "minutes"))

    def test_bulk_actions_touch_only_pending(self):
        order_a = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000101")
        order_b = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000102")
        pending, confirmed = order_a.payment_ids, order_b.payment_ids
        tx = self._store([fio_transaction(901, date.today(), order_b.amount_total, "Order 261-1-000102 2")])
        tx._match_new()
        self.assertEqual(confirmed.fio_state, "confirmed")
        both = pending | confirmed
        both.action_fio_confirm_manual()
        self.assertEqual(pending.fio_state, "manual")
        self.assertEqual(confirmed.fio_state, "confirmed", "hromadné potvrzení nesmí přepsat platbu potvrzenou bankou")
        both.action_fio_reset_pending()
        self.assertEqual((pending.fio_state, confirmed.fio_state), ("pending", "pending"))
        self.assertEqual(tx.state, "new")
        both.action_fio_ignore()
        self.assertEqual((pending.fio_state, confirmed.fio_state), ("ignored", "ignored"))
        both.action_fio_ignore()  # znovu: nic se nerozbije
        (pending | confirmed).action_fio_confirm_manual()
        self.assertEqual(pending.fio_state, "manual", "ignorovanou lze ručně potvrdit")

    def test_refresh_from_bank_button(self):
        Payment = self.env["pos.payment"]
        config = self.pos_config_usd
        order = self._order(self.product_80, self.qr_payment_method, pos_reference="Order 261-1-000066")
        payment = order.payment_ids
        before = Payment.fio_pending_overview(config.id)["count"]
        items = fio_api.parse_transactions(fio_payload([
            fio_transaction(801, date.today(), order.amount_total, "Order 261-1-000066 1"),
        ]))
        self._rest()
        with patch.object(fio_api, "fetch_last", return_value=items) as fetch_last:
            result = Payment.fio_refresh_from_bank(config.id)
            fetch_last.assert_called_once()
        self.assertTrue(result["synced"])
        self.assertEqual(result["count"], before - 1)
        self.assertEqual(payment.fio_state, "confirmed")
        self.assertTrue(config.fio_pos_last_seen)

        with patch.object(fio_api, "fetch_last", return_value=[]) as fetch_last:
            again = Payment.fio_refresh_from_bank(config.id)
            fetch_last.assert_not_called()
        self.assertFalse(again["synced"])
        self.assertGreater(again["wait"], 0, "do 30 s od posledního dotazu se hlásí čekání")
        self.assertEqual(again["next_check_in"], again["wait"], "odpočet v popupu = čekání na Fio")
        self.assertTrue(again["bank_enabled"])
        self.assertEqual(again["interval"], 32)
        self.assertFalse(Payment.fio_pending_overview(self.pos_config_eur.id)["bank_enabled"])

        self._rest()
        with patch.object(fio_api, "fetch_last", side_effect=fio_api.FioApiError("Fio API: HTTP 500 – token")):
            failed = Payment.fio_refresh_from_bank(config.id)
        self.assertFalse(failed["synced"])
        self.assertIn("500", failed["error"])

        self.assertEqual(Payment.fio_refresh_from_bank(self.pos_config_eur.id)["wait"], 0, "pokladna bez QR metody s Fio")
