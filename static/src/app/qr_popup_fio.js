import { QRPopup } from "@point_of_sale/app/components/popups/qr_code_popup/qr_code_popup";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { onMounted, onWillUnmount, useState } from "@odoo/owl";

/** Jak často se pokladna ptá serveru (server sám volá Fio nejvýš jednou za 30 s). */
const POLL_INTERVAL_MS = 5000;

/**
 * Ověření QR platby proti Fio, dokud je otevřený QR kód v pokladně.
 *
 * Server vrací `confirmed`, když na účet dorazil příchozí pohyb pokrývající částku
 * s číslem objednávky ve zprávě (nebo jediný dnešní pohyb v té částce). Pohyb si
 * server rezervuje pro objednávku a spáruje ho při jejím uložení. Popup ukáže zelené
 * „Automaticky potvrzeno“ a pošle stav na zákaznický displej; potvrzení zůstává, dokud
 * obsluha neklikne na „Potvrdit platbu“ (zákazník tak vidí „Platba přijata“ stejně dlouho).
 */
patch(QRPopup.prototype, {
    setup() {
        super.setup();
        this.pos = usePos();
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.fio = useState({ status: "idle", detail: "" });
        this.fioTimer = null;
        this.fioStopped = false;
        onMounted(() => this.fioStart());
        onWillUnmount(() => this.fioStop());
    },

    fioStart() {
        this.fio.status = "checking";
        this.fioCheck();
    },

    fioStop() {
        this.fioStopped = true;
        clearTimeout(this.fioTimer);
        this.fioTimer = null;
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
        this.fioSchedule(POLL_INTERVAL_MS);
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
