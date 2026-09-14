# Fio: ověření QR plateb

Modul pro Odoo 19 (Community). Stahuje pohyby z účtu u **Fio banky** a páruje je s QR
platbami z pokladny a s fakturami. Když je v pokladně otevřený QR kód, ptá se banky
průběžně a při shodě platbu **potvrdí sám** – obsluze i zákazníkovi na displeji se ukáže
„Automaticky potvrzeno“. Stávající platební metoda QR (např. `qr_czk` z modulu
`cz_qr_payment`) zůstává, nová metoda se nezakládá.

## Co modul dělá

| Kde                       | Co se děje                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pokladna, otevřený QR kód | Každých 5 s se pokladna ptá serveru; server stáhne nové pohyby z Fio (nejvýš jednou za 30 s) a hledá příchozí pohyb, který platbu pokrývá (stejná částka nebo vyšší o dýško) a má číslo objednávky ve zprávě. Během čekání běží ukazatel: plný hned po stažení z banky, prázdný ve chvíli další kontroly (30 s), s textem „Další kontrola banky za N s“, takže obsluha ví, zda má smysl počkat. Při shodě: zelené „Automaticky potvrzeno“ (s výší dýška) a banner „Platba přijata“ na zákaznickém displeji – obojí zůstává, dokud obsluha neklikne na „Potvrdit platbu“ (zelené tlačítko). Ruční potvrzení bez čekání na banku je dál možné. |
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
| Ignorováno       | Vědomě vyřazeno z kontroly (storno, chybný záznam).                                                                                                  |

Potvrzení v pokladně („Potvrdit platbu“ u QR kódu) **není** kontrola – platba jen odejde na
pozadí do stavu _Čeká na banku_.

### Jak se páruje

1. **Pokladna** – pohyb je příchozí, **pokrývá platbu** (stejná částka, nebo vyšší – plátce
   přidal dýško; nižší pohyb se nepáruje nikdy) a ve zprávě pro příjemce / identifikaci /
   komentáři je číslo objednávky bez mezer a bez ohledu na velikost písmen a diakritiku.
   Hledá se celé `pos_reference` (`order00275-006-0114`) i jen jeho poslední část
   (`00275-006-0114`), takže sedí zprávy z Odoo 18 („Objednávka …“) i 19 („Order …“) a i když
   plátce začátek zprávy přepíše; před číslem nesmí být další číslice („61-1-000005“ nesedí na
   „261-1-000005“). Rozdíl se uloží k pohybu jako **Dýško** (vidět i u platby).
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
2. V Odoo: _Fakturace › Konfigurace › Bankovní účty_ → účet, na který chodí QR platby
   (účet přiřazený bankovnímu deníku platební metody QR) → záložka **Fio** → vložit token.
   Záložku vidí jen správce systému.
3. **Historie starší 90 dní**: Fio ji přes API vydá jen po dočasném odemknutí – v bankovnictví
   _Nastavení › API_ kliknout u tokenu na **ikonu zámku** a pokyn autorizovat; přístup platí
   **10 minut**. Hned potom v Odoo tlačítko **Načíst historii** (výchozí „Historie od“ = pár dní
   před první QR platbou v pokladně; nastaví instalace). Import běží na pozadí přes cron po
   ročních blocích s pauzou 35 s (dva až tři dotazy), stav ukazuje „Historie stažena do“. Po
   dokončení se na Fio nastaví zarážka za poslední pohyb, aby hodinový `last` navazoval.
   Bez odemknutí skončí import chybou **HTTP 422** a zastaví se; po autorizaci ho stačí spustit
   znovu (už stažené pohyby se přeskočí).
4. Bez importu historie se při prvním stažení jen nastaví zarážka `last` 85 dní zpět (Fio
   jinak sáhne do celé historie a vrátí 422); druhé stažení o 30 s později už pohyby přinese.
5. Zkontrolovat _Prodejní místo › Objednávky › QR platby ke kontrole_ – co se nespárovalo,
   ručně potvrdit nebo ignorovat.

Jeden token má u Fio jednu zarážku `last`. Když stejný token používá lokální testovací Odoo
i produkce, přetahují se o ni a část pohybů dostane jen jedna strana – pro lokální testy
vydat druhý token, nebo ho z lokální databáze po testu smazat.

Cron **Fio: stáhnout nové pohyby a spárovat platby** běží každých 5 minut (Nastavení ›
Technické › Naplánované akce), Fio ale volá jen při běžící pokladně, jinak po hodině. Ručně:
tlačítka **Načíst nové pohyby** a **Spárovat čekající** na účtu; v pokladně stačí otevřít popup
čekajících plateb, který stahuje z banky každých 30 s sám.

V přehledu **QR platby ke kontrole** jsou nad vybranými řádky hromadné akce *Potvrdit ručně*,
*Ignorovat* a *Vrátit ke kontrole*; potvrzení a ignorování mění jen platby ve stavu *Čeká na
banku*, platby potvrzené bankou zůstanou.

## Limity Fio API

Fio dovolí **jeden dotaz za 30 s** na token (jinak HTTP 409). Modul si u účtu pamatuje čas
posledního dotazu a nikdy nevolá dřív – ať se ptá pokladna, cron nebo tlačítko. Souběžné
požadavky řeší zámek řádku účtu. Pokladna při otevřeném QR volá server každých 5 s, ale Fio
se skutečně dotáže jen jednou za 30 s; párování mezitím běží nad už staženými pohyby.

Chybové kódy Fio (dokumentace API Bankovnictví, verze 16. 10. 2025), text chyby je u účtu
v poli „Poslední chyba Fio“:

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
  rozšíření `res.partner.bank` (token, limit, historie), `pos.payment` (`fio_state`,
  `fio_transaction_id`), `pos.payment.method.fio_check_qr_payment()` (RPC pro pokladnu),
  `account.move` / `account.payment` (vazby), průvodce `fio.match.wizard`.
- Fio klient `models/fio_api.py` – čisté funkce (`fetch_last`, `fetch_periods`,
  `set_last_id`, `parse_transactions`), sloupce podle dokumentace Fio
  (`column22` ID, `column0` datum, `column1` částka, `column5` VS, `column16` zpráva,
  `column7` identifikace, `column25` komentář).
- JS: `static/src/app/qr_popup_fio.*` rozšiřuje `QRPopup` pokladny (polling, zelené
  potvrzení, které drží do kliknutí obsluhy); `static/src/app/fio_pending/*` ikona v liště
  (`Navbar`) s počtem čekajících plateb, popup `FioPendingDialog` a obnova po
  `PosStore.syncAllOrders`; `static/src/customer_display/fio_customer_display.*` překryv na
  zákaznickém displeji (bundle `point_of_sale.customer_display_assets`), viditelný dokud
  pokladna posílá `qrPaymentData.fioConfirmed`.
- Nastavení pokladny: _Nespárované QR platby: dny zpět_ (`pos.config.fio_pending_days`,
  výchozí 14, 0 = bez omezení) určuje, co ikona v liště počítá; data dodává
  `pos.payment.fio_pending_overview(config_id)`, tlačítko v popupu
  `pos.payment.fio_refresh_from_bank(config_id)`. Každý dotaz lišty zapíše heartbeat
  `pos.config.fio_pos_last_seen` (nejvýš po 30 s); cron podle něj pozná běžící pokladnu.
- Instalace (`post_init_hook`): všechny dosavadní QR platby → _Čeká na banku_; „Historie od“
  na účtu deníku QR metody.
- Pokladna Odoo 19 načítá u `pos.payment` **všechna pole** (`_load_pos_data_fields` vrací `[]`)
  a při uložení objednávky je posílá zpět – nová platba by přišla s `fio_state = False` a spadla
  na povinném poli. Modul proto pole `fio_*` a `pos_reference` z načítání do pokladny vyřazuje
  a prázdný `fio_state` v `create`/`write` ignoruje (objednávky uložené v prohlížeči před
  aktualizací se tak dosynchronizují).
- Závislosti: `point_of_sale`, `account`; Python `requests` (součást Odoo).

## Testy

```
odoo -d db19test -i fio_qr_payment_validation --test-enable --test-tags /fio_qr_payment_validation --stop-after-init
```

Testy nevolají Fio – používají ukázkovou odpověď API (`tests/fio_payload.py`).
