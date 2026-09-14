import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { patch } from "@web/core/utils/patch";
import { onMounted, onWillUnmount } from "@odoo/owl";
import { FioPendingDialog } from "./fio_pending_dialog";

/** Jak často se počet nespárovaných QR plateb obnovuje ze serveru (navíc po každém uložení objednávek). */
const REFRESH_MS = 60000;

patch(PosStore.prototype, {
    async setup(...args) {
        this.fioPending = { count: 0, payments: [], tipToday: 0, tipTodayCount: 0, tipsUrl: "", loading: false };
        return super.setup(...args);
    },

    /** Načte ze serveru QR platby této pokladny ve stavu „Čeká na banku“ (počet + seznam pro popup). */
    async fioRefreshPending() {
        if (!this.config || this.data.network.offline || this.fioPending.loading) {
            return;
        }
        this.fioPending.loading = true;
        try {
            const result = await this.data.call("pos.payment", "fio_pending_overview", [this.config.id]);
            this.fioPending.count = result.count;
            this.fioPending.payments = result.payments;
            this.fioPending.tipToday = result.tip_today || 0;
            this.fioPending.tipTodayCount = result.tip_today_count || 0;
            this.fioPending.tipsUrl = result.tips_url || "";
        } catch (error) {
            console.warn("Fio: přehled nespárovaných plateb se nenačetl", error);
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
        await this.pos.fioRefreshPending();
        this.env.services.dialog.add(FioPendingDialog, {
            payments: this.pos.fioPending.payments,
            count: this.pos.fioPending.count,
            tipToday: this.pos.fioPending.tipToday,
            tipTodayCount: this.pos.fioPending.tipTodayCount,
            tipsUrl: this.pos.fioPending.tipsUrl,
        });
    },
});
