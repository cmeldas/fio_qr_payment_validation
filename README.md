# Fio: ověření QR plateb

Modul pro Odoo 20 (Community), větev `20.0`; verze pro Odoo 19 je na větvi `19.0`. Stahuje pohyby z účtu u **Fio banky** a páruje je s QR
platbami z pokladny a s fakturami. Když je v pokladně otevřený QR kód, ptá se banky
průběžně a při shodě platbu **potvrdí sám** – obsluze i zákazníkovi na displeji se ukáže
„Automaticky potvrzeno“. Stávající platební metoda QR (např. `qr_czk` z modulu
`cz_qr_payment`) zůstává, nová metoda se nezakládá.

## Co modul dělá

| Kde                       | Co se děje                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pokladna, otevřený QR kód | Každých 5 s se pokladna ptá serveru; server stáhne nové pohyby z Fio (nejvýš jednou za 30 s) a hledá příchozí pohyb, který platbu pokrývá (stejná částka nebo vyšší o dýško) a má číslo objednávky ve zprávě. Během čekání běží ukazatel: plný hned po stažení z banky, prázdný ve chvíli další kontroly (30 s), s textem „Další kontrola banky za N s“, takže obsluha ví, zda má smysl počkat. Při shodě: zelené „Automaticky potvrzeno“ (s výší dýška) a banner „Platba přijata“ na zákaznickém displeji – obojí zůstává, dokud obsluha nepotvrdí platbu v QR dialogu (tlačítko potvrzení zezelená). Ruční potvrzení bez čekání na banku je dál možné. |
| Lišta pokladny            | Ikona peněz s červeným počtem QR plateb této pokladny, které banka ještě nepotvrdila (za posledních N dní podle nastavení pokladny, výchozí 14). Kliknutím popup s datem a časem, číslem objednávky, částkou a tlačítkem _Detail_ (otevře objednávku v seznamu účtenek); dokud je popup otevřený, **stahuje pohyby z banky tak rychle, jak Fio dovolí** (jednou za 30 s) a čekající platby hned páruje; ukazatel odpočítává do dalšího stažení („Další načtení z banky za N s“), vedle je čas posledního stažení a počet spárovaných; dole **Dýška dnes** (součet dýšek z dnešních QR plateb této pokladny) a odkaz na přehled všech dýšek. Počet se obnovuje po každém uložení objednávky a každou minutu; platba zmizí po spárování bankou, ručním potvrzení nebo ignorování v administraci. |
| Dýška                     | _Prodejní místo › Objednávky › Dýška z QR plateb_: pohyby, které přišly vyšší než objednávka, seskupené **po dnech platby v pokladně** se součty; přepínání na pivot (po dnech / týdnech / měsících) a graf, filtr podle pokladny.                                                                                                                                                                                                                                  |
| Uložení objednávky        | Nová QR platba dostane stav **Čeká na banku**. Když už pohyb existuje (rozpoznaný při otevřeném QR nebo stažený cronem), spáruje se hned → **Potvrzeno bankou**.                                                                                                                                                                                                                                                                                           |
| Cron každých 5 minut      | Stáhne nové pohyby (`last`) a spáruje vše, co čeká – včetně plateb potvrzených ručně v pokladně a plateb z víkendu, které banka pošle až v pondělí. Fio se ptá jen když některá pokladna běží (lišta pokladny se hlásí každou minutu, bere se 10 minut zpět); uspaný nebo vypnutý počítač s pokladnou se nehlásí a pak se stahuje jen jednou za hodinu. Ručně potvrzená platba se tak při otevřené pokladně spáruje do 5 minut od připsání. |
| Faktury                   | Příchozí pohyb s variabilním symbolem = platební reference vystavené faktury: na faktuře se **zaregistruje platba** do bankovního deníku účtu (částka a datum z pohybu). Už uhrazená faktura se jen propojí.                                                                                                                                                                                                                                               |
| Historie                  | Po instalaci jde stáhnout celá historie účtu po 90denních blocích (mezi bloky pauza kvůli limitu Fio) a spárovat i staré platby.                                                                                                                                                                                                                                                                                                                           |

### Stavy QR platby v pokladně (`pos.payment`)

| Stav             | Význam                                                                                                                                               |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Čeká na banku    | Platba potvrzená obsluhou, banka pohyb ještě neposlala. Je v přehledu **QR platby ke kontrole**.                                                     |
| Potvrzeno bankou | K platbě je spárovaný pohyb na účtu.                                                                                                                 |
| Potvrzeno ručně  | Obsluha/manažer platbu zkontroloval jinak (výpis, potvrzení plátce). Cron ji dál zkouší spárovat; když pohyb dorazí, změní se na _Potvrzeno bankou_. |
| Ignorováno       | Vědomě vyřazeno z kontroly (storno, chybný záznam, peníze nepřišly). Když peníze s číslem objednávky ve zprávě přece jen dorazí, platba se spáruje a přejde na _Potvrzeno bankou_ (u pohybu poznámka). |

Potvrzení v pokladně (tlačítko potvrzení v QR dialogu) **není** kontrola – platba jen odejde na
pozadí do stavu _Čeká na banku_.

### Jak se páruje

1. **Pokladna** – pohyb je příchozí, **pokrývá platbu** (stejná částka, nebo vyšší – plátce
   přidal dýško; nižší pohyb se nepáruje nikdy) a ve zprávě pro příjemce / identifikaci /
   komentáři je číslo objednávky bez mezer a bez ohledu na velikost písmen a diakritiku.
   Hledá se celé `pos_reference` (`order00275-006-0114`) i jen jeho poslední část
   (`00275-006-0114`), takže sedí zprávy z Odoo 18 („Objednávka …“) i 19 („Order …“) a i když
   plátce začátek zprávy přepíše; před číslem nesmí být další číslice („61-1-000005“ nesedí na
   „261-1-000005“). Rozdíl se uloží k pohybu jako **Dýško** (vidět i u platby). Podle čísla
   objednávky se párují i **ignorované** platby – zákazník občas zaplatí až o dny později; když
   textu odpovídá čekající i ignorovaná platba, má přednost čekající.
2. **Při otevřeném QR** navíc platí záložní pravidlo: když číslo objednávky ve zprávě chybí,
   ale existuje **právě jeden** dnešní nebo včerejší nespárovaný pohyb v částce platby až
   platby + 50 % (dýško bez čísla objednávky), bere se jako platba probíhající objednávky
   (`Jen částka`). Cron toto pravidlo nepoužívá.
3. **Faktury** – variabilní symbol pohybu = číslice z platební reference vystavené faktury
   (`payment_reference`), faktura je zaúčtovaná.

Odchozí pohyby a pohyby bez shody zůstávají **Nespárované** v přehledu _Pohyby Fio_, kde je
lze ručně spárovat (s platbou v pokladně nebo fakturou) nebo ignorovat.

## Nastavení

1. V internetovém bankovnictví Fio: _Nastavení › API_ → vytvořit token k účtu s právy
   **sledování účtu** (čtení). Zřízení se silně autorizuje (SMS / push), token má 64 znaků,
   **funguje až 5 minut po autorizaci** a platí nejdéle 180 dní (volitelně se prodlužuje při
   každém přihlášení do bankovnictví); po vypršení vydat nový a přepsat ho v Odoo.
   **Víc tokenů k jednomu účtu = častější dotazy** (viz Limity Fio API): 2 tokeny 16 s,
   3 tokeny 11 s, 6 tokenů 6 s. Platnost tokenu se podle Fio s automatickým prodlužováním
   obnovuje přihlášením do Internetbankingu nebo Smartbankingu (na 180 dní), ne voláním API.
2. V Odoo: _Fakturace › Konfigurace › Bankovní účty_ → účet, na který chodí QR platby
   (účet přiřazený bankovnímu deníku platební metody QR) → záložka **Fio** → **API tokeny** →
   *Přidat položku*, název (třeba datum vydání) a token, uložit. Záložku vidí jen správce
   systému; hodnota tokenu se po uložení ukazuje jen jako hvězdičky. Pod seznamem je počet
   aktivních tokenů a skutečný interval mezi dotazy. Token jde dočasně vypnout přepínačem
   *Aktivní* (neaktivní se nevolá); u každého je poslední dotaz, poslední úspěšný dotaz,
   zarážka a poslední chyba.
3. **Historie starší 90 dní**: stahuje se **prvním tokenem v seznamu** (pořadí jde přetáhnout).
   Fio ji přes API vydá jen po dočasném odemknutí – v bankovnictví _Nastavení › API_ kliknout
   **u toho tokenu** na **ikonu zámku** a pokyn autorizovat; přístup platí **10 minut** a jen
   pro ten token. Hned potom v Odoo tlačítko **Načíst historii** (výchozí „Historie od“ = pár
   dní před první QR platbou v pokladně; nastaví instalace). Import běží na pozadí přes cron po
   ročních blocích s pauzou 35 s (dva až tři dotazy), stav ukazuje „Historie stažena do“.
   Bez odemknutí skončí import chybou **HTTP 422** a zastaví se; po autorizaci ho stačí spustit
   znovu (už stažené pohyby se přeskočí).
4. Každý token si při svém prvním použití nastaví zarážku `last` 85 dní zpět (Fio jinak sáhne
   do celé historie a vrátí 422) – ten dotaz pohyby ještě nestáhne, další už ano. Co token
   stáhne podruhé (pohyby, které už přinesl jiný token), se podle Fio ID přeskočí. Když se
   token přes 90 dní nepoužije a Fio vrátí 422, zarážku si nastaví znovu sám.
5. Zkontrolovat _Prodejní místo › Objednávky › QR platby ke kontrole_ – co se nespárovalo,
   ručně potvrdit nebo ignorovat.

Lokální testovací Odoo a produkce nesmí sdílet stejné tokeny: dotazy by si navzájem
ubíraly limit 30 s a přetahovaly by se o zarážku `last` (co stáhne kopie, produkce už přes
`last` nedostane). Pro lokální testy vydat vlastní token, nebo ho z lokální databáze po testu
smazat. **Neutralizace** databáze (`odoo neutralize`, obnova zálohy s volbou „neutralize“)
to dělá sama: `data/neutralize.sql` všechny tokeny vypne, jejich hodnotu zahodí a vypne
párování i běžící import historie na účtech (crony vypíná neutralizace jádra).

Cron **Fio: stáhnout nové pohyby a spárovat platby** běží každých 5 minut (Nastavení ›
Technické › Naplánované akce), Fio ale volá jen při běžící pokladně, jinak po hodině. Ručně:
tlačítka **Načíst nové pohyby** a **Spárovat čekající** na účtu; v pokladně stačí otevřít popup
čekajících plateb, který stahuje z banky sám, tak často, jak tokeny dovolí.

V přehledu **QR platby ke kontrole** jsou nad vybranými řádky hromadné akce *Potvrdit ručně*,
*Ignorovat* a *Vrátit ke kontrole*; potvrzení a ignorování mění jen platby ve stavu *Čeká na
banku*, platby potvrzené bankou zůstanou.

## Limity Fio API

Fio dovolí **jeden dotaz za 30 s na token** (jinak HTTP 409), a tokenů může být k účtu víc.
Modul volá **všechny aktivní tokeny postupně za sebou** – vždy ten, který se nejdéle nepoužil
(vystřídají se i při řídkých dotazech a i když některý vrací chybu) – a dotazy
na účet rozkládá rovnoměrně: **interval = 32 s / počet aktivních tokenů** (30 s limit + 2 s
rezerva, protože Odoo ukládá čas na celé sekundy a Fio lokálně odmítlo dotaz i po 32 s),
zaokrouhleno nahoru, nejméně 2 s (1 token 32 s, 2 tokeny 16 s, 3 tokeny 11 s, 4 tokeny 8 s,
6 tokenů 6 s). Žádný token se nepoužije dřív než po 32 s, ať se ptá pokladna, cron nebo
tlačítko; souběžné požadavky řeší zámek řádku účtu. Pokladna při otevřeném QR se ptá serveru přesně ve chvíli dalšího
dotazu (nejpozději po 5 s) a ukazatel s textem „Další kontrola banky za N s (každých M s)“
ukazuje skutečný interval; stejně tak popup čekajících plateb. Párování mezi dotazy běží nad
už staženými pohyby.

Chybové kódy Fio (dokumentace API Bankovnictví, verze 16. 10. 2025), text chyby je u tokenu
v seznamu a u účtu v poli „Poslední chyba Fio“ (s názvem tokenu):

| kód | význam                                  | co s tím                                                        |
| --- | --------------------------------------- | --------------------------------------------------------------- |
| 409 | druhý dotaz do 30 s                     | modul počká, nic dělat                                          |
| 422 | data starší 90 dní bez silné autorizace | ikona zámku u tokenu v bankovnictví, do 10 minut spustit znovu  |
| 500 | neexistující nebo neaktivní token       | zkontrolovat token, u nového počkat 5 minut, u starého platnost |
| 413 | přes 50 000 pohybů v jednom dotazu      | zkrátit „Historie od“                                           |
| 404 | špatný tvar dotazu                      | chyba modulu, hlásit                                            |

## Menu a pohledy

- _Prodejní místo › Objednávky › QR platby ke kontrole_ – QR platby pokladny podle stavu
  ověření, výchozí filtr _Čeká na banku_, řádkové akce potvrdit ručně / ignorovat / vrátit.
- _Prodejní místo › Objednávky › Pohyby Fio_ – všechny stažené pohyby, výchozí filtr
  _Nespárované příchozí_, akce ručně spárovat / ignorovat / zrušit spárování.
- _Prodejní místo › Objednávky › Platby_ – nový sloupec a filtry _Ověření Fio_.
- Objednávka pokladny – sloupec stavu u plateb; faktura – chytré tlačítko _Pohyby Fio_.

## Technicky

- Modely: `fio.bank.transaction` (pohyby, unikátní `bank_account_id` + `fio_id`),
  `fio.api.token` (tokeny účtu: hodnota jen pro `base.group_system`, vlastní `last_request_at`,
  `bookmark_set`, `last_error`), rozšíření `res.partner.bank` (`fio_token_ids`, výběr tokenu
  `_fio_pick_token`, rozestup `_fio_interval_seconds`, historie), `pos.payment` (`fio_state`,
  `fio_transaction_id`), `pos.payment.method.fio_check_qr_payment()` (RPC pro pokladnu),
  `account.move` / `account.payment` (vazby), průvodce `fio.match.wizard`.
- Fio klient `models/fio_api.py` – čisté funkce (`fetch_last`, `fetch_periods`,
  `set_last_id`, `parse_transactions`), sloupce podle dokumentace Fio
  (`column22` ID, `column0` datum, `column1` částka, `column5` VS, `column16` zpráva,
  `column7` identifikace, `column25` komentář).
- JS (OWL 3): `static/src/app/qr_popup_fio.*` rozšiřuje `QRPopup` pokladny (polling, zelené
  potvrzení, které drží do kliknutí obsluhy) a `PosPayment.getQrPopupProps()` (UUID platby
  `fioPaymentUuid` a příznak `fioConfirmed`); `static/src/app/fio_pending/*` ikona v liště
  (`Navbar`) s počtem čekajících plateb, popup `FioPendingDialog` a obnova po
  `PosStore.syncAllOrders`; `static/src/customer_display/fio_customer_display.xml` překryv na
  zákaznickém displeji (bundle `point_of_sale.customer_display_assets`), viditelný dokud
  pokladna v datech QR posílá `fioConfirmed`.
- Nastavení pokladny: _Nespárované QR platby: dny zpět_ (`pos.config.fio_pending_days`,
  výchozí 14, 0 = bez omezení) určuje, co ikona v liště počítá; data dodává
  `pos.payment.fio_pending_overview(config_id)`, tlačítko v popupu
  `pos.payment.fio_refresh_from_bank(config_id)`. Každý dotaz lišty zapíše heartbeat
  `pos.config.fio_pos_last_seen` (nejvýš po 30 s); cron podle něj pozná běžící pokladnu.
- Instalace (`post_init_hook`): všechny dosavadní QR platby → _Čeká na banku_; „Historie od“
  na účtu deníku QR metody.
- Pokladna Odoo 19 i 20 načítá u `pos.payment` **všechna pole** (`_load_pos_data_fields` vrací `[]`)
  a při uložení objednávky je posílá zpět – nová platba by přišla s `fio_state = False` a spadla
  na povinném poli. Modul proto pole `fio_*` a `pos_reference` z načítání do pokladny vyřazuje
  a prázdný `fio_state` v `create`/`write` ignoruje (objednávky uložené v prohlížeči před
  aktualizací se tak dosynchronizují).
- Závislosti: `point_of_sale`, `account`; Python `requests` (součást Odoo).

## Testy

```
docker compose -f odoo20/docker-compose.yml run --rm odoo odoo -c /etc/odoo/odoo.conf -d t20_fio --db-filter='^t20_fio$' \
    -i fio_qr_payment_validation --without-demo=all --test-enable --test-tags /fio_qr_payment_validation --stop-after-init
```

Testy nevolají Fio – používají ukázkovou odpověď API (`fio_payload()` v `tests/test_fio_matching.py`).
Ověřují i neutralizaci (`data/neutralize.sql`) a migrační skript 19 → 20.

## Odoo 20

Port z 19.0.1.2.0 na **20.0.1.2.0** (chování stejné, data modulu beze změny):

- **Práva**: `security/ir.model.access.csv` → `security/ir.access.csv` (nový model `ir.access`,
  sloupce `operation` = `r` / `crud`, prázdná `domain`); stejná XML ID i skupiny jako v 19.
- **Typ QR platební metody** se v Odoo 20 jmenuje `bank_qr_code` (v 19 `qr_code`); upravené
  rozpoznání QR plateb (`pos.payment._is_fio_qr_payment`, účty pokladny pro lištu) i instalační
  hook. Účet je `res.partner.bank.account_number` (v 19 `acc_number`).
- **Pokladna (OWL 3)**: `QRPopup` je v 20 obecný dialog bez platebního řádku v props
  (`line`, `order` zmizely); modul si přidá `fioPaymentUuid` do `getQrPopupProps()` a řádek si
  najde podle UUID, polling běží jen u bankovních QR plateb (online platby a terminály se
  nemění). Rozšíření šablony míří na `point_of_sale.QRPopup` (dřív
  `point_of_sale.QRConfirmationDialog`); tlačítko potvrzení je v 20 výchozí modré, po potvrzení
  bankou zezelená. Šablony používají `this.` (OWL 3 nepřebírá vlastnosti komponenty jako
  proměnné), `useState` → `proxy`, `static props` → `useProps`, `env.utils.formatCurrency` →
  `pos.formatCurrency`.
- **Zákaznický displej** dostává v 20 QR jako `qrData` z `getQrPopupProps()` vybraného platebního
  řádku; modul do nich přidá `fioConfirmed` (z `uiState` řádku) a displej překreslí. Úprava
  `CustomerFacingQR.props` (komponenta v 20 není) odpadla.
- **Ikony**: Odoo 20 už nemá FontAwesome; tlačítka ve views (`icon="account_balance"`, `link`,
  `block`, `undo`, `check`) i šablony pokladny (`<i class="oi" data-icon="…"/>`) používají
  Material Symbols.
- **Neutralizace**: nový `data/neutralize.sql` (viz výše), v 19 chyběl.

### Migrace dat 19 → 20

`migrations/20.0.1.2.0/pre-migrate.py` (jen SQL, běží před načtením dat modulu):

1. Smaže XML ID modulu, která v `ir_model_data` ukazují na zrušené `ir.model.access` /
   `ir.rule` (i staré řádky práv, pokud tabulka ještě je); jinak by načtení `ir.access.csv` se
   stejnými XML ID spadlo na „record of different model“. Práva pak modul založí znovu.
   Pokud je už převedl migrační rámec (XML ID ukazují na `ir.access`), nic nedělá.
2. Pojistka za skripty `point_of_sale`: `pos_payment_method.payment_method_type`
   `qr_code` → `bank_qr_code` (na produkci metoda „QR“, `qr_czk`). Bez toho pokladna QR kód
   nezobrazí. Když hodnotu převedly migrační skripty jádra, nic nemění.

Vlastní tabulky a sloupce modulu (`fio_bank_transaction`, `fio_api_token`, `pos_payment.fio_*`,
`res_partner_bank.fio_*`, `pos_config.fio_*`, `account_payment.fio_transaction_id`) se nemění.
Skript `19.0.1.2.0` zůstává: produkce, která by šla na 20 ještě z 19.0.1.1.0 (token na účtu),
dostane převod tokenu do seznamu tokenů ve stejném běhu. Od skriptů jádra modul potřebuje jen
to, aby bankovní účty (`res.partner.bank`), platby pokladny a platební metody zůstaly se stejnými
id; přejmenování `acc_number` → `account_number` a zrušení `res.bank` řeší skripty jádra
a `cz_qr_payment`.
