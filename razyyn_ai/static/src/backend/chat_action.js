/** @odoo-module **/
/* Copyright (c) 2026, Marwan Badr and contributors
 * For license information, please see LICENSE
 *
 * Razyyn AI inside Odoo's own web client.
 *
 * WHY THIS EXISTS
 *     The menu used to be an "open this URL in a new window" action, so
 *     clicking Razyyn AI threw the customer out of Odoo into a bare page with
 *     no menu, no breadcrumbs and no way back to their settings. It is a client
 *     action now, which is the Odoo equivalent of what the chat is on ERPNext:
 *     a page of the application, with the application still around it.
 *
 * WHY THE CHAT IS IN A FRAME
 *     Because it brings its own stylesheet -- the same one ERPNext serves, two
 *     and a half thousand lines of it -- and Odoo brings Bootstrap. Dropped
 *     into the web client directly, each would restyle the other: Odoo's button
 *     rules would land on the chat's composer, and the chat's :root colours
 *     would land on Odoo's forms. A frame is a boundary the browser enforces
 *     for free, and it keeps the two products' chat windows byte-identical,
 *     which is the whole point of the port.
 */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillUnmount, onMounted, useRef } from "@odoo/owl";

export class RazyynChatAction extends Component {
    static template = "razyyn_ai.ChatAction";
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.frame = useRef("frame");

        // The chat asks to open an Odoo record -- "Connected as ..." opens the
        // connection form. It cannot do that itself from inside a frame, so it
        // asks out here, where the breadcrumbs are.
        this.onMessage = (event) => {
            if (event.origin !== window.location.origin) return;
            const data = event.data || {};
            if (data.razyyn !== "open_record" || !data.record) return;
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: data.record.model,
                res_id: data.record.res_id,
                views: [[false, "form"]],
                target: "current",
            });
        };

        onMounted(() => window.addEventListener("message", this.onMessage));
        onWillUnmount(() => window.removeEventListener("message", this.onMessage));
    }
}

registry.category("actions").add("razyyn_ai.chat", RazyynChatAction);
