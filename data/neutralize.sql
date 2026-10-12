-- Fio: neutralizovaná kopie (lokální vývoj, test) se nesmí ptát banky produkčními tokeny.
-- Token sdílí s produkcí limit jednoho dotazu za 30 s a hlavně zarážku `last`: co by stáhla
-- kopie, to by produkce už nikdy nedostala. Tokeny se proto vypnou a jejich hodnota zahodí
-- (pole je povinné, zůstane neplatná náhrada). Crony vypíná neutralizace modulu base.
UPDATE fio_api_token
   SET active = false,
       token = 'neutralized-' || id,
       bookmark_set = false,
       last_request_at = NULL,
       last_error = 'Neutralizovaná databáze: token odstraněn, pro test vlož vlastní.';

UPDATE res_partner_bank
   SET fio_enabled = false,
       fio_history_running = false
 WHERE fio_enabled OR fio_history_running;
