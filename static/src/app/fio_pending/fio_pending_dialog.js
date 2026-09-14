import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";

/** Jak často se překresluje odpočet do dalšího možného dotazu na banku. */
const TICK_MS = 250;

/**
 * Seznam QR plateb, které banka ještě nepotvrdila (stav „Čeká na banku“), pro obsluhu
 * pokladny: datum a čas, číslo objednávky, částka, proklik na objednávku v seznamu účtenek,
 * dýška za dnešek a tlačítko „Načíst platby z banky“ s ukazatelem, za jak dlouho Fio dovolí
 * další dotaz (během limitu se načtení odloží a proběhne samo). Data čte přímo
 * z `pos.fioPending`, takže se seznam po načtení z banky sám překreslí.
 */
export class FioPendingDialog extends Component {
    static template = "fio_qr_payment_validation.FioPendingDialog";
    static components = { Dialog };
    static props = { close: Function };

    setup() {
        this.pos = usePos();
        this.title = _t("QR platby čekající na banku");
        this.clock = useState({ now: Date.now() });
        let ticker = null;
        onMounted(() => {
            ticker = setInterval(() => {
                this.clock.now = Date.now();
            }, TICK_MS);
        });
        onWillUnmount(() => clearInterval(ticker));
    }

    get pending() {
        return this.pos.fioPending;
    }

    get remainingMs() {
        return Math.max(0, this.pending.nextAllowedAt - this.clock.now);
    }

    /** Šířka ukazatele: 100 % hned po dotazu na banku, 0 % když je další dotaz možný. */
    get progressPercent() {
        const total = (this.pending.rateLimit || 30) * 1000;
        return Math.min(100, Math.round((this.remainingMs / total) * 100));
    }

    get countdownLabel() {
        const seconds = Math.ceil(this.remainingMs / 1000);
        if (this.pending.loading) {
            return _t("Načítám z banky…");
        }
        if (seconds <= 0) {
            return _t("Banka je připravená na další dotaz.");
        }
        return this.pending.autoRefresh
            ? _t("Načtu automaticky za %s s.", seconds)
            : _t("Banka dovolí další dotaz za %s s.", seconds);
    }

    formatDate(value) {
        return value ? deserializeDateTime(value).toFormat("d. M. yyyy HH:mm") : "";
    }

    formatAmount(value) {
        return this.env.utils.formatCurrency(value || 0);
    }

    orderLabel(row) {
        return row.pos_reference || row.order_name || row.tracking_number || "";
    }

    async refreshFromBank() {
        await this.pos.fioRefreshFromBank();
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
