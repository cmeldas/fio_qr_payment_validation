import { Component, onMounted, onWillUnmount, proxy, t, useProps } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";

/** Jak často se překresluje odpočet a kontroluje, zda už je čas na další stažení z banky. */
const TICK_MS = 250;

/**
 * Seznam QR plateb, které banka ještě nepotvrdila (stav „Čeká na banku“), pro obsluhu
 * pokladny: datum a čas, číslo objednávky, částka, proklik na objednávku v seznamu účtenek
 * a dýška za dnešek. Dokud je popup otevřený, stahuje pohyby z banky tak rychle, jak Fio dovolí
 * (32 s děleno počtem tokenů účtu) – ukazatel odpočítává do dalšího stažení. Data čte přímo z `pos.fioPending`,
 * takže se seznam po každém stažení sám překreslí.
 */
export class FioPendingDialog extends Component {
    static template = "fio_qr_payment_validation.FioPendingDialog";
    static components = { Dialog };
    props = useProps({ close: t.function() });

    setup() {
        this.pos = usePos();
        this.title = _t("QR platby čekající na banku");
        this.clock = proxy({ now: Date.now() });
        let ticker = null;
        onMounted(() => {
            ticker = setInterval(() => {
                this.clock.now = Date.now();
                if (this.pending.bankEnabled && !this.pending.loading && this.remainingMs() <= 0) {
                    this.pos.fioRefreshFromBank();
                }
            }, TICK_MS);
        });
        onWillUnmount(() => clearInterval(ticker));
    }

    get pending() {
        return this.pos.fioPending;
    }

    remainingMs() {
        return Math.max(0, this.pending.nextAllowedAt - this.clock.now);
    }

    /** Šířka ukazatele: 100 % hned po stažení z banky, 0 % ve chvíli dalšího stažení. */
    progressPercent() {
        const total = (this.pending.interval || 30) * 1000;
        return Math.min(100, Math.round((this.remainingMs() / total) * 100));
    }

    countdownLabel() {
        const seconds = Math.ceil(this.remainingMs() / 1000);
        if (this.pending.loading) {
            return _t("Načítám z banky…");
        }
        return seconds > 0 ? _t("Další načtení z banky za %s s", seconds) : _t("Načítám z banky…");
    }

    /** Skutečný interval mezi dotazy na banku (32 s / počet Fio tokenů). */
    intervalLabel() {
        return _t("každých %s s", this.pending.interval || 30);
    }

    formatDate(value) {
        return value ? deserializeDateTime(value).toFormat("d. M. yyyy HH:mm") : "";
    }

    formatAmount(value) {
        return this.pos.formatCurrency(value || 0);
    }

    orderLabel(row) {
        return row.pos_reference || row.order_name || row.tracking_number || "";
    }

    /** Přehled všech dýšek po dnech (administrace) v nové záložce. */
    openTips() {
        if (this.pending.tipsUrl) {
            window.open(this.pending.tipsUrl, "_blank");
        }
    }

    openOrder(row) {
        this.props.close();
        this.pos.navigate("TicketScreen", {
            stateOverride: {
                filter: "SYNCED",
                search: {
                    fieldName: "RECEIPT_NUMBER",
                    searchTerm: row.pos_reference || row.order_name,
                },
            },
        });
    }
}
