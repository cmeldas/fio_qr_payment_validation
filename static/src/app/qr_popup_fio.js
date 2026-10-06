import { QRPopup } from "@point_of_sale/app/components/popups/qr_code_popup/qr_code_popup";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { onMounted, onWillUnmount, useState } from "@odoo/owl";

/** Nejdelší pauza mezi dotazy pokladny na server (server sám volá Fio nejvýš jednou za 30 s). */
const POLL_INTERVAL_MS = 5000;
/** Rezerva, aby dotaz na server nepřišel o zlomek sekundy dřív, než Fio zase dovolí stažení. */
const DUE_MARGIN_MS = 300;
/** Jak často se překresluje odpočet do další kontroly banky. */
const TICK_MS = 250;

/**
 * Ověření QR platby proti Fio, dokud je otevřený QR kód v pokladně.
 *
 * Server vrací `confirmed`, když na účet dorazil příchozí pohyb pokrývající částku
 * s číslem objednávky ve zprávě (nebo jediný dnešní pohyb v té částce). Pohyb si
 * server rezervuje pro objednávku a spáruje ho při jejím uložení. Popup ukáže zelené
 * „Automaticky potvrzeno“ a pošle stav na zákaznický displej; potvrzení zůstává, dokud
 * obsluha neklikne na „Potvrdit platbu“ (zákazník tak vidí „Platba přijata“ stejně dlouho).
 *
 * Během čekání běží ukazatel: plný hned po stažení z banky, prázdný ve chvíli dalšího
 * dotazu – přesně tehdy se pokladna serveru zeptá znovu. Délku dává server: 30 s děleno
 * počtem Fio tokenů účtu (tokeny se střídají), např. 10 s při třech tokenech.
 */
patch(QRPopup.prototype, {
    setup() {
        super.setup();
        this.pos = usePos();
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.fio = useState({
            status: "idle",
            detail: "",
            nextAt: 0, // kdy Fio zase dovolí dotaz (ms)
            interval: 30, // s mezi dotazy na banku (30 s / počet tokenů)
            now: Date.now(),
        });
        this.fioTimer = null;
        this.fioTicker = null;
        this.fioStopped = false;
        onMounted(() => this.fioStart());
        onWillUnmount(() => this.fioStop());
    },

    fioStart() {
        this.fio.status = "checking";
        this.fioTicker = setInterval(() => {
            this.fio.now = Date.now();
        }, TICK_MS);
        this.fioCheck();
    },

    fioStop() {
        this.fioStopped = true;
        clearTimeout(this.fioTimer);
        clearInterval(this.fioTicker);
        this.fioTimer = null;
        this.fioTicker = null;
    },

    fioSchedule(delayMs) {
        if (this.fioStopped) {
            return;
        }
        clearTimeout(this.fioTimer);
        this.fioTimer = setTimeout(() => this.fioCheck(), delayMs);
    },

    fioOrderReference() {
        const order = this.props.order;
        return order.pos_reference || order.name || "";
    },

    /** Zbývající ms do chvíle, kdy Fio dovolí další stažení. */
    get fioRemainingMs() {
        return Math.max(0, this.fio.nextAt - this.fio.now);
    },

    /** Šířka ukazatele: 100 % hned po stažení z banky, 0 % když je další dotaz na řadě. */
    get fioProgressPercent() {
        const total = (this.fio.interval || 30) * 1000;
        return Math.min(100, Math.round((this.fioRemainingMs / total) * 100));
    },

    get fioCountdownLabel() {
        const seconds = Math.ceil(this.fioRemainingMs / 1000);
        return seconds > 0 ? _t("Další kontrola banky za %s s", seconds) : _t("Kontroluji banku…");
    },

    /** Skutečný interval mezi dotazy na banku, např. „každých 10 s“. */
    get fioIntervalLabel() {
        return _t("každých %s s", this.fio.interval || 30);
    },

    async fioCheck() {
        if (this.fioStopped || this.fio.status === "confirmed") {
            return;
        }
        const line = this.props.line;
        let result;
        try {
            result = await this.orm.call("pos.payment.method", "fio_check_qr_payment", [
                [line.payment_method_id.id],
                line.amount,
                this.fioOrderReference(),
                this.pos.currency.id,
            ]);
        } catch (error) {
            console.warn("Fio: ověření platby selhalo", error);
            if (this.fioStopped) {
                return;
            }
            this.fio.status = "error";
            this.fio.detail = _t("Ověření v bance teď není dostupné, potvrď platbu ručně.");
            this.fioSchedule(POLL_INTERVAL_MS * 3);
            return;
        }
        if (this.fioStopped) {
            return;
        }
        if (!result.enabled) {
            this.fio.status = "disabled";
            return;
        }
        if (result.confirmed) {
            this.fioConfirmed(result);
            return;
        }
        this.fio.status = "checking";
        this.fio.detail = "";
        this.fio.interval = result.interval || 30;
        this.fio.nextAt = Date.now() + (result.next_check_in || 0) * 1000;
        this.fio.now = Date.now();
        // další dotaz nejpozději za 5 s, ale přesně ve chvíli, kdy Fio dovolí nové stažení
        // (nejdřív za 1 s, aby se pokladna nezacyklila, když server stažení z nějakého důvodu neudělá)
        const untilDue = this.fioRemainingMs + DUE_MARGIN_MS;
        this.fioSchedule(Math.min(POLL_INTERVAL_MS, Math.max(1000, untilDue)));
    },

    fioConfirmed(result) {
        this.fio.status = "confirmed";
        this.fio.detail = result.counter_name
            ? _t("Platba od %s dorazila na účet.", result.counter_name)
            : _t("Platba dorazila na účet.");
        if (result.tip > 0) {
            this.fio.detail += " " + _t("Dýško %s.", this.env.utils.formatCurrency(result.tip));
        }
        const line = this.props.line;
        line.qrPaymentData = { ...(line.qrPaymentData || {}), fioConfirmed: true };
        this.notification.add(_t("Automaticky potvrzeno: platba dorazila na účet Fio."), {
            type: "success",
        });
        clearTimeout(this.fioTimer);
        this.fioTimer = null;
    },
});
