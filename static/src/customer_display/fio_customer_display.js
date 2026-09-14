import { CustomerFacingQR } from "@point_of_sale/customer_display/customer_facing_qr";

// Pokladna přidává do dat QR platby příznak fioConfirmed; dialog s QR ho musí v props tolerovat.
// Překryv „Platba přijata“ řeší rozšíření šablony point_of_sale.CustomerDisplay: drží se, dokud
// obsluha v pokladně potvrzení neodklikne (pokladna pak pošle qrPaymentData = null).
CustomerFacingQR.props = {
    ...CustomerFacingQR.props,
    fioConfirmed: { type: Boolean, optional: true },
};
