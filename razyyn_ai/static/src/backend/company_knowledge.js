/** @odoo-module **/
/* Copyright (c) 2026, Marwan Badr and contributors
 * For license information, please see LICENSE
 *
 * Company accounting knowledge, on the Agent Settings form.
 *
 * WHAT IS ON THIS CARD
 *     The company's own accounting policy, and the country whose law the agent
 *     applies. Both are read by the agent before it answers a question about
 *     how THIS business keeps its books, so what is here is what the agent
 *     believes about the business.
 *
 * WHY IT IS A FIELD WIDGET AND NOT A PAGE
 *     Because it belongs beside the connection it applies to. On ERPNext it is
 *     a card inside Agent Settings; putting it on a menu of its own here would
 *     be the two products diverging over a screen, which is the thing this
 *     module exists not to do.
 *
 * WHY THE COUNTRY IS A LIST AND NOT A TWO-LETTER BOX
 *     It selects which country's accounting requirements are retrieved. A
 *     mistyped code is not a cosmetic slip -- "AE" typed for Egypt answers
 *     under the wrong jurisdiction, and nothing anywhere reports an error. The
 *     list comes from the platform, so this file holds no copy of it.
 */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onWillStart, useState, useRef } from "@odoo/owl";

const MAX_BYTES = 40 * 1024 * 1024;

/**
 * The card's four calls, made with `fetch` and nothing else.
 *
 * WHY NOT `useService("rpc")`
 *     Because this file is ONE file for two framework generations, and that
 *     service does not exist in both: Odoo 18 removed it, so a card that
 *     rendered correctly on 17 threw "Service rpc is not available" on 18 and
 *     the whole settings page went blank. Every framework service this shared
 *     code touches is a way for the two products to diverge, and `fetch` is
 *     not going anywhere.
 */
async function ask(path, fields) {
    const options = { method: "GET" };
    if (fields) {
        const body = new FormData();
        body.append("csrf_token", odoo.csrf_token);
        for (const [key, value] of Object.entries(fields)) body.append(key, value);
        Object.assign(options, { method: "POST", body });
    }
    try {
        const response = await fetch(path, options);
        return await response.json();
    } catch (error) {
        return {
            ok: false,
            error: "Could not reach this Odoo server. Check your connection and try again.",
        };
    }
}

export class CompanyKnowledge extends Component {
    static template = "razyyn_ai.CompanyKnowledge";
    static props = { ...standardFieldProps };

    setup() {
        this.fileInput = useRef("file");
        this.state = useState({
            loading: true,
            /** The platform's own sentence when something is refused. */
            error: "",
            notice: "",
            /** What is indexed right now, or null. */
            policy: null,
            countries: [],
            country: "",
            /** The country in the box, which may not be the saved one yet. */
            chosen: "",
            chosenFile: "",
            busy: false,
            progress: "",
        });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        const result = await ask("/razyyn/knowledge/catalogue");
        this.state.loading = false;
        if (!result.ok) {
            this.state.error = result.error;
            return;
        }
        this.state.error = "";
        this.state.countries = result.countries || [];
        this.state.country = result.country_code || "";
        this.state.chosen = this.state.country;
        this.state.policy =
            (result.documents || []).find((d) => d.scope === "company") || null;
    }

    get countryChanged() {
        return this.state.chosen && this.state.chosen !== this.state.country;
    }

    onCountryChange(event) {
        this.state.chosen = event.target.value;
        this.state.notice = "";
        this.state.error = "";
    }

    /**
     * Saved on a button rather than on change: the platform rate-limits this
     * route -- it is the same one that changes a password -- and a dropdown
     * that wrote on every scroll would spend that budget on nothing.
     */
    async saveCountry() {
        const code = this.state.chosen;
        this.state.busy = true;
        const result = await ask("/razyyn/knowledge/country", { country_code: code });
        this.state.busy = false;
        if (!result.ok) {
            this.state.error = result.error;
            return;
        }
        this.state.country = code;
        this.state.error = "";
        this.state.notice =
            "Country saved. The agent now applies this country's requirements.";
    }

    chooseFile() {
        this.fileInput.el.click();
    }

    onFileChosen(event) {
        const file = event.target.files[0];
        this.state.notice = "";
        if (!file) {
            this.state.chosenFile = "";
            return;
        }
        if (!/\.pdf$/i.test(file.name)) {
            this.state.error =
                "That is not a PDF. Export your policy as a PDF and try again.";
            this.state.chosenFile = "";
            return;
        }
        if (file.size > MAX_BYTES) {
            this.state.error = `That PDF is ${(file.size / 1048576).toFixed(
                1
            )} MB. The limit is 40 MB.`;
            this.state.chosenFile = "";
            return;
        }
        this.state.error = "";
        this.state.chosenFile = `${file.name} (${(file.size / 1048576).toFixed(1)} MB)`;
    }

    /**
     * XHR rather than fetch, for the one thing fetch cannot report: upload
     * progress. A 40 MB policy over a slow office link is otherwise a minute
     * of a disabled button and a still page.
     */
    uploadPolicy() {
        const file = this.fileInput.el.files[0];
        if (!file) return;

        const form = new FormData();
        form.append("csrf_token", odoo.csrf_token);
        form.append("title", "Company accounting policy");
        form.append("file", file, file.name);

        this.state.busy = true;
        this.state.error = "";
        this.state.notice = "";
        this.state.progress = "Sending...";

        const request = new XMLHttpRequest();
        request.open("POST", "/razyyn/knowledge/policy");
        request.upload.onprogress = (event) => {
            if (!event.lengthComputable) return;
            const percent = Math.round((event.loaded / event.total) * 100);
            this.state.progress =
                percent < 100
                    ? `Sending... ${percent}%`
                    : "Reading the PDF and indexing it — this can take a minute...";
        };
        request.onload = async () => {
            this.state.busy = false;
            this.state.progress = "";
            let body = {};
            try {
                body = JSON.parse(request.responseText || "{}");
            } catch (e) {
                body = {};
            }
            if (!body.ok) {
                this.state.error = body.error || "The upload was refused.";
                return;
            }
            this.state.chosenFile = "";
            this.fileInput.el.value = "";
            this.state.notice = `Policy indexed: ${body.page_count ?? "?"} pages, ${
                body.chunk_count ?? "?"
            } searchable sections.`;
            await this.load();
        };
        request.onerror = () => {
            this.state.busy = false;
            this.state.progress = "";
            this.state.error =
                "The upload did not reach the server. Check your connection and try again.";
        };
        request.send(form);
    }

    async removePolicy() {
        if (!this.state.policy) return;
        this.state.busy = true;
        const result = await ask("/razyyn/knowledge/policy/delete", {
            document_id: this.state.policy.document_id,
        });
        this.state.busy = false;
        if (!result.ok) {
            this.state.error = result.error;
            return;
        }
        this.state.notice = "Policy removed. The agent has stopped applying it.";
        await this.load();
    }
}

export const companyKnowledgeField = {
    component: CompanyKnowledge,
    supportedTypes: ["char"],
};

registry.category("fields").add("razyyn_company_knowledge", companyKnowledgeField);
