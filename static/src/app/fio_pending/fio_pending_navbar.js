import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { patch } from "@web/core/utils/patch";
import { onMounted, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { FioPendingDialog } from "./fio_pending_dialog";

/**
 * Jak často se počet nespárovaných QR plateb obnovuje ze serveru (navíc po každém uložení
 * objednávek). Dotaz zároveň slouží serveru jako znamení, že pokladna běží a není uspaná:
 * dokud chodí, cron se Fio ptá každých 5 minut, jinak jednou za hodinu.
 */
const REFRESH_MS = 60000;

patch(PosStore.prototype, {
    async setup(...args) {
        this.fioPending = {
            count: 0,
            payments: [],
            tipToday: 0,
            tipTodayCount: 0,
            tipsUrl: "",
            loading: false,
            message: "",
            messageType: "",
        };
        return super.setup(...args);
    },

    fioApplyOverview(result) {
        this.fioPending.count = result.count;
        this.fioPending.payments = result.payments;
        this.fioPending.tipToday = result.tip_today || 0;
        this.fioPending.tipTodayCount = result.tip_today_count || 0;
        this.fioPending.tipsUrl = result.tips_url || "";
    },

    /** Načte ze serveru QR platby této pokladny ve stavu „Čeká na banku“ (počet + seznam pro popup). */
    async fioRefreshPending() {
        if (!this.config || this.data.network.offline || this.fioPending.loading) {
            return;
        }
        this.fioPending.loading = true;
        try {
            const result = await this.data.call("pos.payment", "fio_pending_overview", [this.config.id]);
            this.fioApplyOverview(result);
        } catch (error) {
            console.warn("Fio: přehled nespárovaných plateb se nenačetl", error);
        } finally {
            this.fioPending.loading = false;
        }
    },

    /** Tlačítko v popupu: stáhnout nové pohyby z Fio (limit 30 s), spárovat čekající a obnovit seznam. */
    async fioRefreshFromBank() {
        if (!this.config || this.fioPending.loading) {
            return;
        }
        if (this.data.network.offline) {
            this.fioPending.message = _t("Pokladna je offline.");
            this.fioPending.messageType = "danger";
            return;
        }
        this.fioPending.loading = true;
        this.fioPending.message = "";
        try {
            const before = this.fioPending.count;
            const result = await this.data.call("pos.payment", "fio_refresh_from_bank", [this.config.id]);
            this.fioApplyOverview(result);
            const matched = Math.max(0, before - result.count);
            if (result.synced) {
                this.fioPending.message = matched
                    ? _t("Načteno z banky, spárováno %s.", matched)
                    : _t("Načteno z banky, nic nového ke spárování.");
                this.fioPending.messageType = matched ? "success" : "";
            } else if (result.wait) {
                this.fioPending.message = _t("Banka dovolí další dotaz za %s s.", result.wait);
                this.fioPending.messageType = "warning";
            } else if (result.error) {
                this.fioPending.message = result.error;
                this.fioPending.messageType = "danger";
            } else {
                this.fioPending.message = _t("Žádný účet s Fio tokenem u QR metod této pokladny.");
                this.fioPending.messageType = "warning";
            }
        } catch (error) {
            console.warn("Fio: načtení z banky selhalo", error);
            this.fioPending.message = _t("Načtení z banky selhalo.");
            this.fioPending.messageType = "danger";
        } finally {
            this.fioPending.loading = false;
        }
    },

    async syncAllOrders(options = {}) {
        const result = await super.syncAllOrders(options);
        this.fioRefreshPending();
        return result;
    },
});

patch(Navbar.prototype, {
    setup() {
        super.setup();
        let timer = null;
        onMounted(() => {
            this.pos.fioRefreshPending();
            timer = setInterval(() => this.pos.fioRefreshPending(), REFRESH_MS);
        });
        onWillUnmount(() => clearInterval(timer));
    },

    async openFioPending() {
        this.pos.fioPending.message = "";
        this.pos.fioPending.messageType = "";
        await this.pos.fioRefreshPending();
        this.env.services.dialog.add(FioPendingDialog, {});
    },
});
