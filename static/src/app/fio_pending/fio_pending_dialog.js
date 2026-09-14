import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";

/**
 * Seznam QR plateb, které banka ještě nepotvrdila (stav „Čeká na banku“), pro obsluhu
 * pokladny: datum a čas, číslo objednávky, částka, proklik na objednávku v seznamu účtenek,
 * dýška za dnešek a tlačítko „Načíst platby z banky“. Data čte přímo z `pos.fioPending`,
 * takže se seznam po načtení z banky sám překreslí.
 */
export class FioPendingDialog extends Component {
    static template = "fio_qr_payment_validation.FioPendingDialog";
    static components = { Dialog };
    static props = { close: Function };

    setup() {
        this.pos = usePos();
        this.title = _t("QR platby čekající na banku");
    }

    get pending() {
        return this.pos.fioPending;
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
