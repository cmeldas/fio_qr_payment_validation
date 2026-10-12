import {
    QRPopup,
    qrPopupProps,
} from "@point_of_sale/app/components/popups/qr_code_popup/qr_code_popup";
import { PosPayment } from "@point_of_sale/app/models/pos_payment";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { onMounted, onWillUnmount, proxy, t } from "@odoo/owl";

/** Nejdelší pauza mezi dotazy pokladny na server (server sám volá Fio nejvýš jednou za 30 s). */
const POLL_INTERVAL_MS = 5000;
/** Rezerva, aby dotaz na server nepřišel o zlomek sekundy dřív, než Fio zase dovolí stažení. */
const DUE_MARGIN_MS = 300;
/** Jak často se překresluje odpočet do další kontroly banky. */
const TICK_MS = 250;

/*
 * Odoo 20: QRPopup je obecný dialog, který dostane jen hodnoty z `PosPayment.getQrPopupProps()`
 * (stejné hodnoty jdou i na zákaznický displej, proto jen jednoduché typy, žádné záznamy).
 * Modul k nim přidá UUID platby, podle kterého popup najde platební řádek, a příznak
 * potvrzení bankou, podle kterého zákaznický displej ukáže „Platba přijata“.
 */
Object.assign(qrPopupProps, {
    fioPaymentUuid: t.string().optional(),
    fioConfirmed: t.boolean().optional(),
});

patch(PosPayment.prototype, {
    getQrPopupProps() {
        const props = super.getQrPopupProps(...arguments);
        if (!this.useBankQrCode) {
            return props;
        }
        return {
            ...props,
            fioPaymentUuid: this.uuid,
            fioConfirmed: Boolean(this.uiState?.fioConfirmed),
        };
    },
});

/**
 * Ověření QR platby proti Fio, dokud je otevřený QR kód v pokladně.
 *
 * Server vrací `confirmed`, když na účet dorazil příchozí pohyb pokrývající částku
 * s číslem objednávky ve zprávě (nebo jediný dnešní pohyb v té částce). Pohyb si
 * server rezervuje pro objednávku a spáruje ho při jejím uložení. Popup ukáže zelené
 * „Automaticky potvrzeno“ a pošle stav na zákaznický displej; potvrzení zůstává, dokud
 * obsluha neklikne na potvrzení platby (zákazník tak vidí „Platba přijata“ stejně dlouho).
 *
 * Během čekání běží ukazatel: plný hned po stažení z banky, prázdný ve chvíli dalšího
 * dotazu – přesně tehdy se pokladna serveru zeptá znovu. Délku dává server: 32 s děleno
 * počtem Fio tokenů účtu (tokeny se střídají), např. 11 s při třech tokenech.
 *
 * Popup bez `fioPaymentUuid` (online platby, platební terminály) se nemění.
 */
patch(QRPopup.prototype, {
    setup() {
        super.setup(...arguments);
        this.pos = usePos();
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.fio = proxy({
            status: "idle",
            detail: "",
            nextAt: 0, // kdy Fio zase dovolí dotaz (ms)
            interval: 30, // s mezi dotazy na banku (32 s / počet tokenů)
            now: Date.now(),
        });
        this.fioTimer = null;
        this.fioTicker = null;
        this.fioStopped = false;
        onMounted(() => this.fioStart());
        onWillUnmount(() => this.fioStop());
    },

    /** Platební řádek, ke kterému QR kód patří (null u QR, které nejsou bankovní QR platbou). */
    fioLine() {
        const uuid = this.props.fioPaymentUuid;
        return (uuid && this.pos.models["pos.payment"].getBy("uuid", uuid)) || null;
    },

    fioStart() {
        if (!this.fioLine()) {
            return;
        }
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
        // QR se zavřel (potvrzeno nebo zrušeno): příště začíná ověření znovu
        const line = this.fioLine();
        if (line?.uiState?.fioConfirmed) {
            line.uiState.fioConfirmed = false;
        }
    },

    fioSchedule(delayMs) {
        if (this.fioStopped) {
            return;
        }
        clearTimeout(this.fioTimer);
        this.fioTimer = setTimeout(() => this.fioCheck(), delayMs);
    },

    fioOrderReference(line) {
        const order = line.pos_order_id;
        return order.pos_reference || order.name || "";
    },

    /** Zbývající ms do chvíle, kdy Fio dovolí další stažení. */
    fioRemainingMs() {
        return Math.max(0, this.fio.nextAt - this.fio.now);
    },

    /** Šířka ukazatele: 100 % hned po stažení z banky, 0 % když je další dotaz na řadě. */
    fioProgressPercent() {
        const total = (this.fio.interval || 30) * 1000;
        return Math.min(100, Math.round((this.fioRemainingMs() / total) * 100));
    },

    fioCountdownLabel() {
        const seconds = Math.ceil(this.fioRemainingMs() / 1000);
        return seconds > 0 ? _t("Další kontrola banky za %s s", seconds) : _t("Kontroluji banku…");
    },

    /** Skutečný interval mezi dotazy na banku, např. „každých 11 s“. */
    fioIntervalLabel() {
        return _t("každých %s s", this.fio.interval || 30);
    },

    async fioCheck() {
        if (this.fioStopped || this.fio.status === "confirmed") {
            return;
        }
        const line = this.fioLine();
        if (!line) {
            this.fio.status = "idle";
            return;
        }
        let result;
        try {
            result = await this.orm.call("pos.payment.method", "fio_check_qr_payment", [
                [line.payment_method_id.id],
                line.amount,
                this.fioOrderReference(line),
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
            this.fioConfirmed(line, result);
            return;
        }
        this.fio.status = "checking";
        this.fio.detail = "";
        this.fio.interval = result.interval || 30;
        this.fio.nextAt = Date.now() + (result.next_check_in || 0) * 1000;
        this.fio.now = Date.now();
        // další dotaz nejpozději za 5 s, ale přesně ve chvíli, kdy Fio dovolí nové stažení
        // (nejdřív za 1 s, aby se pokladna nezacyklila, když server stažení z nějakého důvodu neudělá)
        const untilDue = this.fioRemainingMs() + DUE_MARGIN_MS;
        this.fioSchedule(Math.min(POLL_INTERVAL_MS, Math.max(1000, untilDue)));
    },

    fioConfirmed(line, result) {
        this.fio.status = "confirmed";
        this.fio.detail = result.counter_name
            ? _t("Platba od %s dorazila na účet.", result.counter_name)
            : _t("Platba dorazila na účet.");
        if (result.tip > 0) {
            this.fio.detail += " " + _t("Dýško %s.", this.pos.formatCurrency(result.tip));
        }
        // zákaznický displej čte stav z getQrPopupProps() vybraného platebního řádku
        line.uiState = line.uiState || {};
        line.uiState.fioConfirmed = true;
        this.pos.debounceUpdateCustomerDisplay?.();
        this.notification.add(_t("Automaticky potvrzeno: platba dorazila na účet Fio."), {
            type: "success",
        });
        clearTimeout(this.fioTimer);
        this.fioTimer = null;
    },
});
