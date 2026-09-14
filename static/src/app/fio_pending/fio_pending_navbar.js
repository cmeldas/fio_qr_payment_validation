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
/** Rezerva, aby dotaz na server nepřišel dřív, než Fio zase dovolí stažení. */
const DUE_MARGIN_MS = 300;

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
            rateLimit: 30, // s
            autoRefresh: false, // obsluha klikla na načtení během limitu: načte se samo, až to půjde
            loading: false,
            message: "",
            messageType: "",
        };
        this.fioAutoTimer = null;
        return super.setup(...args);
    },

    fioApplyOverview(result) {
        this.fioPending.count = result.count;
        this.fioPending.payments = result.payments;
        this.fioPending.tipToday = result.tip_today || 0;
        this.fioPending.tipTodayCount = result.tip_today_count || 0;
        this.fioPending.tipsUrl = result.tips_url || "";
        this.fioPending.bankEnabled = Boolean(result.bank_enabled);
        this.fioPending.rateLimit = result.rate_limit || 30;
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

    fioArmAutoRefresh(delayMs) {
        this.fioPending.autoRefresh = true;
        clearTimeout(this.fioAutoTimer);
        this.fioAutoTimer = setTimeout(() => this.fioRefreshFromBank(), delayMs + DUE_MARGIN_MS);
    },

    fioCancelAutoRefresh() {
        this.fioPending.autoRefresh = false;
        clearTimeout(this.fioAutoTimer);
        this.fioAutoTimer = null;
    },

    /**
     * Tlačítko v popupu: stáhnout nové pohyby z Fio, spárovat čekající a obnovit seznam.
     * Když Fio ještě nedovolí další dotaz (30 s), počká se a načte se samo, jakmile to půjde.
     */
    async fioRefreshFromBank() {
        if (!this.config) {
            return;
        }
        if (this.data.network.offline) {
            this.fioCancelAutoRefresh();
            this.fioPending.message = _t("Pokladna je offline.");
            this.fioPending.messageType = "danger";
            return;
        }
        const waitMs = this.fioWaitMs();
        if (waitMs > 0) {
            this.fioArmAutoRefresh(waitMs);
            this.fioPending.message = "";
            return;
        }
        if (this.fioPending.loading) {
            this.fioArmAutoRefresh(1000);
            return;
        }
        clearTimeout(this.fioAutoTimer);
        this.fioPending.loading = true;
        this.fioPending.message = "";
        try {
            const before = this.fioPending.count;
            const result = await this.data.call("pos.payment", "fio_refresh_from_bank", [this.config.id]);
            this.fioApplyOverview(result);
            const matched = Math.max(0, before - result.count);
            if (result.synced) {
                this.fioCancelAutoRefresh();
                this.fioPending.message = matched
                    ? _t("Načteno z banky, spárováno %s.", matched)
                    : _t("Načteno z banky, nic nového ke spárování.");
                this.fioPending.messageType = matched ? "success" : "";
            } else if (result.wait) {
                // limit obsadil jiný požadavek (cron, druhá pokladna): počkat a zkusit znovu
                this.fioArmAutoRefresh(result.wait * 1000);
                this.fioPending.message = "";
            } else if (result.error) {
                this.fioCancelAutoRefresh();
                this.fioPending.message = result.error;
                this.fioPending.messageType = "danger";
            } else {
                this.fioCancelAutoRefresh();
                this.fioPending.message = _t("Žádný účet s Fio tokenem u QR metod této pokladny.");
                this.fioPending.messageType = "warning";
            }
        } catch (error) {
            console.warn("Fio: načtení z banky selhalo", error);
            this.fioCancelAutoRefresh();
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
