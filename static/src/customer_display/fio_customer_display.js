import { CustomerDisplay } from "@point_of_sale/customer_display/customer_display";
import { CustomerFacingQR } from "@point_of_sale/customer_display/customer_facing_qr";
import { patch } from "@web/core/utils/patch";
import { useEffect, useState } from "@odoo/owl";

/** Jak dlouho zůstane na displeji „Platba přijata“ po potvrzení bankou. */
const BANNER_MS = 8000;

// Pokladna přidává do dat QR platby příznak fioConfirmed; dialog s QR ho musí v props tolerovat.
CustomerFacingQR.props = {
    ...CustomerFacingQR.props,
    fioConfirmed: { type: Boolean, optional: true },
};

/**
 * Zákaznický displej: přes QR kód se překryje zelené „Platba přijata“, když pokladna
 * pošle potvrzení z banky. Zůstane i po zavření QR kódu, aby ho zákazník stihl přečíst.
 */
patch(CustomerDisplay.prototype, {
    setup() {
        super.setup();
        this.fio = useState({ confirmed: false });
        let timer = null;
        useEffect(
            (confirmed) => {
                if (confirmed) {
                    this.fio.confirmed = true;
                    clearTimeout(timer);
                    timer = setTimeout(() => {
                        this.fio.confirmed = false;
                    }, BANNER_MS);
                }
            },
            () => [Boolean(this.order.qrPaymentData && this.order.qrPaymentData.fioConfirmed)]
        );
    },
});
