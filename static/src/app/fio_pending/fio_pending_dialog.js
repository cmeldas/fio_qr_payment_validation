import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";

/**
 * Seznam QR plateb, které banka ještě nepotvrdila (stav „Čeká na banku“), pro obsluhu
 * pokladny: datum a čas, číslo objednávky, částka, proklik na objednávku v seznamu účtenek.
 */
export class FioPendingDialog extends Component {
    static template = "fio_qr_payment_validation.FioPendingDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        payments: Array,
        count: Number,
        tipToday: { type: Number, optional: true },
        tipTodayCount: { type: Number, optional: true },
        tipsUrl: { type: String, optional: true },
    };
    static defaultProps = { tipToday: 0, tipTodayCount: 0, tipsUrl: "" };

    setup() {
        this.pos = usePos();
        this.title = _t("QR platby čekající na banku");
    }

    /** Přehled všech dýšek po dnech (administrace) v nové záložce. */
    openTips() {
        if (this.props.tipsUrl) {
            window.open(this.props.tipsUrl, "_blank");
        }
    }

    formatDate(value) {
        return value ? deserializeDateTime(value).toFormat("d. M. yyyy HH:mm") : "";
    }

    formatAmount(value) {
        return this.env.utils.formatCurrency(value);
    }

    orderLabel(row) {
        return row.pos_reference || row.order_name || row.tracking_number || "";
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
