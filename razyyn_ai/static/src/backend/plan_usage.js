/** @odoo-module **/
/* Copyright (c) 2026, Marwan Badr and contributors
 * For license information, please see LICENSE
 *
 * The plan usage bar, on the Agent Settings form.
 *
 * WHAT IT IS
 *     How much of this month's budget the account has spent, and which plan it
 *     is spending -- the same card ERPNext customers already have on their own
 *     Agent Settings, so the two products say the same thing about a bill.
 *
 * WHY THE FIGURE IS NOT A Field ON THE RECORD
 *     It belongs to the platform. It changes with every request the agent
 *     answers, on any of the customer's devices, and it is the basis of a bill.
 *     A copy stored in this Odoo would be stale by however long ago it was
 *     written, on the one card whose whole job is to warn the customer BEFORE
 *     their next request is refused.
 *
 * WHY A FAILURE IS NOT A RED BOX
 *     A card that gives up shows a customer an error about their balance
 *     whenever the platform is merely busy, which is exactly when they came to
 *     look at it. An unreadable figure says so quietly, in one line, and
 *     offers Retry; nothing else on the settings screen is affected.
 *
 * WHY `fetch` AND NOT `useService("rpc")`
 *     This file is ONE file for two framework generations, and that service
 *     does not exist in both: Odoo 18 removed it, so a card that rendered on 17
 *     threw "Service rpc is not available" on 18 and blanked the whole page.
 */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onWillStart, useState } from "@odoo/owl";

/** Where a customer goes to buy more of their monthly budget.
 *
 *  The Frappe app names the same address once in its own settings script. One
 *  hard-coded copy per product is the most that should ever exist.
 */
const PRICING_URL = "https://razyyn.com/pricing/";

/** The three bands the bar is read in, and the one place their colours live.
 *
 *  These were three parallel nested ternaries in the Frappe card once, one per
 *  CSS property, and a value none of them named silently showed the wrong
 *  colour. A band is one row here.
 */
const BANDS = [
    { from: 90, tone: "danger" },
    { from: 70, tone: "warning" },
    { from: 0, tone: "ok" },
];

export class PlanUsage extends Component {
    static template = "razyyn_ai.PlanUsage";
    static props = { ...standardFieldProps };

    setup() {
        this.pricingUrl = PRICING_URL;
        this.state = useState({
            loading: true,
            /** False when the platform could not be read. Never an exception. */
            known: false,
            error: "",
            percentage: 0,
            plan: "free",
        });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        let result;
        try {
            const response = await fetch("/razyyn/usage", { method: "GET" });
            result = await response.json();
        } catch {
            result = {
                known: false,
                error: "Could not reach this Odoo server. Check your connection and try again.",
            };
        }
        Object.assign(this.state, {
            loading: false,
            known: !!result.known,
            error: result.known ? "" : (result.error || "Razyyn AI could not be reached."),
            percentage: Number(result.total_usage_percentage || 0),
            plan: result.plan || "free",
        });
    }

    /** Read it again now. Its own method rather than an inline arrow, matching
     *  the knowledge card beside it, and so the template names an action
     *  rather than a closure. */
    refresh() {
        return this.load();
    }

    /** Never past the end of the bar, however far past the plan the account is. */
    get width() {
        return Math.max(0, Math.min(this.state.percentage, 100));
    }

    get tone() {
        return BANDS.find((band) => this.state.percentage >= band.from).tone;
    }
}

export const planUsageField = {
    component: PlanUsage,
    supportedTypes: ["char"],
};

registry.category("fields").add("razyyn_plan_usage", planUsageField);
