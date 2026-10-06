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
/** Když stažení z banky neproběhlo a server neřekl, za jak dlouho to půjde, zkusit znovu za tolik ms. */
const RETRY_MS = 5000;

patch(PosStore.prototype, {
    async setup(...args) {
        this.fioPending = {
            count: 0,
            payments: [],
            tipToday: 0,
            tipTodayCount: 0,
            tipsUrl: "",
            bankEnabled: false,
            nextAllowedAt: 0, // kdy Fio zase dovolí dotaz (ms)
            interval: 30, // s mezi dotazy na banku (30 s / počet tokenů)
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
        this.fioPending.bankEnabled = Boolean(result.bank_enabled);
        this.fioPending.interval = result.interval || 30;
        this.fioPending.nextAllowedAt = Date.now() + (result.next_check_in || 0) * 1000;
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

    /**
     * Za kolik ms Fio dovolí další dotaz (0 = hned). Metoda, ne getter: getter přidaný patchem
     * na reaktivní PosStore vracel pořád první spočítanou hodnotu.
     */
    fioWaitMs() {
        return Math.max(0, this.fioPending.nextAllowedAt - Date.now());
    },

    /**
     * Stáhne nové pohyby z Fio, spáruje čekající a obnoví seznam. Volá otevřený popup čekajících
     * plateb vždy, když odpočet dojede na nulu (Fio dovolí jeden dotaz za 30 s); dřív se nic neděje.
     */
    async fioRefreshFromBank() {
        if (!this.config || this.fioPending.loading || this.fioWaitMs() > 0) {
            return;
        }
        if (this.data.network.offline) {
            this.fioPending.message = _t("Pokladna je offline.");
            this.fioPending.messageType = "danger";
            this.fioPending.nextAllowedAt = Date.now() + RETRY_MS;
            return;
        }
        this.fioPending.loading = true;
        try {
            const before = this.fioPending.count;
            const result = await this.data.call("pos.payment", "fio_refresh_from_bank", [this.config.id]);
            this.fioApplyOverview(result);
            const matched = Math.max(0, before - result.count);
            const time = new Date().toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
            if (result.synced) {
                this.fioPending.message = matched
                    ? _t("Načteno z banky v %s, spárováno %s.", time, matched)
                    : _t("Načteno z banky v %s, nic nového.", time);
                this.fioPending.messageType = matched ? "success" : "";
            } else if (result.error) {
                this.fioPending.message = result.error;
                this.fioPending.messageType = "danger";
            } else if (!result.wait) {
                // účet právě dotazuje někdo jiný nebo se teprve nastavila zarážka: zkusit za chvíli
                this.fioPending.nextAllowedAt = Math.max(this.fioPending.nextAllowedAt, Date.now() + RETRY_MS);
            }
        } catch (error) {
            console.warn("Fio: načtení z banky selhalo", error);
            this.fioPending.message = _t("Načtení z banky selhalo.");
            this.fioPending.messageType = "danger";
            this.fioPending.nextAllowedAt = Date.now() + RETRY_MS;
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
