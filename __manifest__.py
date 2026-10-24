# -*- coding: utf-8 -*-
{
    "name": "Fio: ověření QR plateb",
    "summary": "Párování QR plateb z pokladny a faktur s pohyby na účtu u Fio banky, automatické potvrzení v pokladně",
    "description": """
Fio: ověření QR plateb
======================

Stahuje pohyby z účtu u Fio banky (REST API, token jen pro čtení) a páruje je:

* **pokladna** – QR platby (metoda s QR kódem) podle čísla objednávky ve zprávě
  pro příjemce a částky. Když je v pokladně otevřený QR kód, server se ptá banky
  (nejvýš jednou za 30 s kvůli limitu Fio) a při shodě se platba potvrdí sama:
  obsluze i na zákaznickém displeji se ukáže „Automaticky potvrzeno“. Ruční
  potvrzení zůstává; taková platba čeká na kontrolu, dokud ji cron nespáruje.
* **faktury** – podle variabilního symbolu (platební reference) a částky;
  při shodě se na faktuře automaticky zaregistruje platba do bankovního deníku.

Každou hodinu (a tlačítkem) se stáhnou nové pohyby a spárují i platby potvrzené
ručně – o víkendu dorazí platby často až v pondělí. Po instalaci jde stáhnout
celá historie účtu a spárovat i staré platby. Nespárované QR platby pokladny
jsou v přehledu ke kontrole, kde je lze ručně potvrdit nebo ignorovat.
""",
    "version": "20.0.1.2.1",
    "category": "Sales/Point of Sale",
    "author": "Matouš Čmelík",
    "website": "https://github.com/cmeldas/fio_qr_payment_validation",
    "license": "LGPL-3",
    "depends": ["point_of_sale", "account"],
    "data": [
        "security/ir.access.csv",
        "data/ir_cron.xml",
        "views/fio_bank_transaction_views.xml",
        "views/pos_payment_views.xml",
        "views/res_partner_bank_views.xml",
        "views/res_config_settings_views.xml",
        "views/account_move_views.xml",
        "wizard/fio_match_wizard_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "fio_qr_payment_validation/static/src/app/**/*",
        ],
        "point_of_sale.customer_display_assets": [
            "fio_qr_payment_validation/static/src/customer_display/**/*",
        ],
    },
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
    "auto_install": False,
}
