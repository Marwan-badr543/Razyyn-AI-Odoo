/* Copyright (c) 2026, Marwan Badr and contributors
 * For license information, please see LICENSE
 *
 * THE SEVENTEEN CALLS.
 *
 * static/src/chat/frappe/ is the Razyyn chat window, copied out of the Frappe
 * app byte for byte by tools/sync_from_frappe.py. Eight thousand lines of it
 * reach their platform through exactly seventeen `frappe.*` entry points and a
 * `__()`. This file is those seventeen, answered by Odoo.
 *
 * WHY A SHIM RATHER THAN A SECOND CHAT WINDOW
 *     Because the alternative was tried. The Odoo module had its own page --
 *     seven hundred lines against the Frappe app's eight thousand -- and it
 *     could not stream, saved no history, and reached one desk out of six. Not
 *     because anybody did it badly, but because a second implementation of a
 *     product's main screen is a promise to re-fix every bug twice, and that
 *     promise is never kept.
 *
 *     So: one chat window, two platforms, and the difference between them is
 *     this file. A change to the chat is made once, in the Frappe app, and
 *     `sync_from_frappe.py --check` fails the build if Odoo has fallen behind.
 *
 * NOTHING HERE MAY KNOW WHAT THE CHAT IS FOR.
 *     This file knows about alerts, dialogs, RPC and a websocket-shaped event
 *     stream. It must never learn what a clarification is, or what a plan card
 *     looks like, because the moment it does there are two places to fix a chat
 *     bug again and the shim has become the thing it exists to prevent.
 */

(function () {
	"use strict";

	// ─── Where a Frappe method name lands on Odoo ───────────────────────────
	//
	// Frappe addresses server code by its full dotted Python path. Odoo
	// addresses it by URL. The mapping is "the last segment of the path", and
	// it is spelled out rather than computed so that a method this module has
	// NOT implemented fails loudly, naming itself, instead of quietly 404ing
	// behind a `catch` and leaving the customer with a screen that does
	// nothing. Every name here has a route of the same name in
	// controllers/chat_client_api.py.
	const RPC_BASE = "/razyyn/api/";
	const KNOWN_METHODS = new Set([
		"authenticate_agent",
		"cancel_agent",
		"create_chat_with_id",
		"delete_agent_account",
		"delete_chat",
		"disconnect_agent",
		"edit_message",
		"get_active_banner_message",
		"get_agent_settings_name",
		"get_chat_history",
		"get_chats",
		"get_connection_status",
		"get_run_state",
		"get_turn_result",
		"get_upload_rules",
		"get_user_usage",
		"send_message",
		"update_chat_title",
		"upload_agent_file",
	]);

	function method_url(dotted_path) {
		const name = String(dotted_path || "").split(".").pop();
		if (!KNOWN_METHODS.has(name)) {
			throw new Error(
				"razyyn_platform: the chat window called '" + dotted_path + "', which " +
				"this module does not implement. Add a /razyyn/api/" + name + " route " +
				"in controllers/chat_client_api.py and list it in KNOWN_METHODS."
			);
		}
		return RPC_BASE + name;
	}

	// ─── Translation ────────────────────────────────────────────────────────
	//
	// Same contract as Frappe's `__`: look the source string up, fall back to
	// the source string itself, then substitute {0}, {1}... Odoo's own
	// catalogue is consulted when the page has published one; an untranslated
	// build shows English, which is what Frappe does too.
	function translate(text, args) {
		const table = window.RAZYYN_TRANSLATIONS || {};
		let out = table[text] || text;
		if (args && args.length) {
			args.forEach(function (value, index) {
				out = out.replace(new RegExp("\\{" + index + "\\}", "g"), value);
			});
		}
		return out;
	}

	// ─── Small DOM helpers, so the shim carries no stylesheet of its own ────
	function element(tag, className, html) {
		const node = document.createElement(tag);
		if (className) node.className = className;
		if (html !== undefined) node.innerHTML = html;
		return node;
	}

	function escape_html(text) {
		const node = document.createElement("div");
		node.appendChild(document.createTextNode(text === undefined || text === null ? "" : String(text)));
		return node.innerHTML;
	}

	// ─── Toasts ─────────────────────────────────────────────────────────────
	function show_alert(options, seconds) {
		const spec = typeof options === "string" ? { message: options } : (options || {});
		let host = document.getElementById("razyyn-alert-host");
		if (!host) {
			host = element("div", "razyyn-alert-host");
			host.id = "razyyn-alert-host";
			document.body.appendChild(host);
		}
		const toast = element("div", "razyyn-alert razyyn-alert-" + (spec.indicator || "blue"));
		toast.innerHTML = spec.message === undefined ? "" : String(spec.message);
		host.appendChild(toast);
		window.setTimeout(function () {
			toast.classList.add("razyyn-alert-leaving");
			window.setTimeout(function () { toast.remove(); }, 250);
		}, Math.max(1, Number(seconds) || 5) * 1000);
	}

	// ─── Freeze ─────────────────────────────────────────────────────────────
	//
	// Counted, not a boolean. The chat freezes around nested awaits ("Sending",
	// then "Uploading" inside it); a boolean lets the inner unfreeze release
	// the outer one and the customer types into a page that is still working.
	let freeze_depth = 0;
	function freeze(message) {
		freeze_depth += 1;
		let veil = document.getElementById("razyyn-freeze");
		if (!veil) {
			veil = element("div", "razyyn-freeze");
			veil.id = "razyyn-freeze";
			veil.appendChild(element("div", "razyyn-freeze-message"));
			document.body.appendChild(veil);
		}
		veil.querySelector(".razyyn-freeze-message").textContent = message || translate("Working...");
		veil.classList.add("razyyn-freeze-on");
	}

	function unfreeze() {
		freeze_depth = Math.max(0, freeze_depth - 1);
		if (freeze_depth > 0) return;
		const veil = document.getElementById("razyyn-freeze");
		if (veil) veil.classList.remove("razyyn-freeze-on");
	}

	// ─── Modal ──────────────────────────────────────────────────────────────
	//
	// One implementation behind Dialog, confirm and prompt, because the chat
	// window uses all three and three lookalikes would drift apart visually.
	class RazyynModal {
		constructor(options) {
			this.options = options || {};
			this.wrapper = element("div", "razyyn-modal-backdrop");
			const box = element("div", "razyyn-modal");
			const head = element("div", "razyyn-modal-head");
			head.appendChild(element("div", "razyyn-modal-title", escape_html(this.options.title || "")));
			const close = element("button", "razyyn-modal-close", "&times;");
			close.type = "button";
			close.setAttribute("aria-label", translate("Close"));
			close.addEventListener("click", () => this.hide());
			head.appendChild(close);
			box.appendChild(head);

			this.body = element("div", "razyyn-modal-body");
			box.appendChild(this.body);
			this.footer = element("div", "razyyn-modal-foot");
			box.appendChild(this.footer);
			this.wrapper.appendChild(box);

			this.wrapper.addEventListener("mousedown", (event) => {
				if (event.target === this.wrapper) this.hide();
			});
			this._on_key = (event) => { if (event.key === "Escape") this.hide(); };

			(this.options.fields || []).forEach((field) => this._render_field(field));

			// jQuery handle, because the copied chat window reaches into the
			// dialog it just opened with `d.$wrapper.find(...)`.
			this.$wrapper = window.jQuery ? window.jQuery(this.wrapper) : null;
		}

		_render_field(field) {
			if (field.fieldtype === "HTML") {
				this.body.appendChild(element("div", "razyyn-modal-html", field.options || ""));
				return;
			}
			const row = element("div", "razyyn-modal-field");
			if (field.label) {
				const label = element("label", null, escape_html(field.label));
				label.setAttribute("for", "razyyn-field-" + field.fieldname);
				row.appendChild(label);
			}
			const input = element(field.fieldtype === "Text" ? "textarea" : "input");
			input.id = "razyyn-field-" + field.fieldname;
			input.className = "razyyn-modal-input";
			input.name = field.fieldname;
			if (field.fieldtype !== "Text") input.type = "text";
			if (field.default !== undefined && field.default !== null) input.value = field.default;
			if (field.reqd) input.required = true;
			row.appendChild(input);
			this.body.appendChild(row);
		}

		set_primary_action(label, handler) {
			const button = element("button", "razyyn-btn razyyn-btn-primary", escape_html(label));
			button.type = "button";
			button.addEventListener("click", () => handler(this.get_values()));
			this.footer.appendChild(button);
			return button;
		}

		set_secondary_action(label, handler) {
			const button = element("button", "razyyn-btn", escape_html(label));
			button.type = "button";
			button.addEventListener("click", () => handler());
			this.footer.insertBefore(button, this.footer.firstChild);
			return button;
		}

		get_values() {
			const values = {};
			this.body.querySelectorAll(".razyyn-modal-input").forEach((input) => {
				values[input.name] = input.value;
			});
			return values;
		}

		show() {
			document.body.appendChild(this.wrapper);
			document.addEventListener("keydown", this._on_key);
			const first = this.body.querySelector(".razyyn-modal-input");
			if (first) window.setTimeout(() => first.focus(), 30);
		}

		hide() {
			document.removeEventListener("keydown", this._on_key);
			this.wrapper.remove();
		}
	}

	function confirm_dialog(message, on_yes, on_no) {
		const modal = new RazyynModal({ title: translate("Confirm") });
		modal.body.appendChild(element("div", "razyyn-modal-message", String(message || "")));
		modal.set_secondary_action(translate("No"), () => {
			modal.hide();
			if (on_no) on_no();
		});
		modal.set_primary_action(translate("Yes"), () => {
			modal.hide();
			if (on_yes) on_yes();
		});
		modal.show();
		return modal;
	}

	function prompt_dialog(fields, callback, title, primary_label) {
		const list = Array.isArray(fields) ? fields : [fields];
		const modal = new RazyynModal({ title: title || translate("Enter Value"), fields: list });
		modal.set_primary_action(primary_label || translate("Submit"), (values) => {
			const missing = list.some((field) => field.reqd && !String(values[field.fieldname] || "").trim());
			if (missing) {
				show_alert({ message: translate("Please fill in the required fields."), indicator: "orange" });
				return;
			}
			modal.hide();
			callback(values);
		});
		modal.show();
		return modal;
	}

	// ─── Saying what the server said ────────────────────────────────────────
	//
	// THE CHAT WINDOW DOES NOT SHOW ITS OWN ERRORS, AND IT IS RIGHT NOT TO.
	// Twenty-four of its catch blocks do nothing but `console.error(err)`,
	// because on Frappe the customer has ALREADY been told: frappe/request.js
	// takes `_server_messages` off the reply and calls frappe.msgprint before
	// the promise rejects, so by the time a catch runs there is a dialog on
	// screen. Rejecting quietly here made all twenty-four silent at once.
	//
	// The first one anybody hit was the sign-in card. The password was wrong,
	// the server said so in as many words, Odoo returned it with HTTP 200 --
	// and the page did not move. Nothing on screen, nothing in the status line,
	// the request "succeeded". That is what this restores.
	let open_message = null;

	function hide_msgprint() {
		if (open_message) {
			open_message.hide();
			open_message = null;
		}
	}

	function msgprint(options) {
		const spec = typeof options === "string" ? { message: options } : (options || {});
		const text = String(spec.message === undefined ? "" : spec.message);

		// Replacing rather than stacking, which is what frappe.hide_msgprint()
		// ahead of every msgprint amounts to. A poll that fails every second
		// must not bury the page under a hundred identical dialogs.
		if (open_message && open_message.razyyn_message === text) return open_message;
		hide_msgprint();

		const modal = new RazyynModal({ title: spec.title || translate("Message") });
		modal.razyyn_message = text;
		const body = element("div", "razyyn-modal-message razyyn-modal-" +
			(spec.indicator || "blue"));
		// The server's sentence, as text. It is a sentence written for a
		// person, not markup, and it is not this shim's job to decide it is
		// safe to render.
		body.textContent = text;
		modal.body.appendChild(body);
		modal.set_primary_action(translate("Close"), () => hide_msgprint());
		const close = modal.hide.bind(modal);
		modal.hide = function () { close(); if (open_message === modal) open_message = null; };
		modal.show();
		open_message = modal;
		return modal;
	}

	// ─── RPC ────────────────────────────────────────────────────────────────
	//
	// A refusal must arrive as a sentence. Odoo answers a refused request with
	// {"error": "..."} and a non-2xx status; Frappe's own xcall rejects with a
	// message, and the chat window's catch blocks expect `.message` to be
	// something it can show a person.
	// Odoo's own call shape. A route declared type="json" is reached with a
	// JSON-RPC envelope and answers with one, and a refusal comes back as
	// `error` with HTTP 200 -- so the status line says nothing and the body
	// must be read either way.
	//
	// It is also why these routes need no CSRF token: a page on another origin
	// cannot send `Content-Type: application/json` without a preflight, and
	// Odoo approves none.
	async function post_json(url, args) {
		let response;
		try {
			response = await fetch(url, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				credentials: "same-origin",
				body: JSON.stringify({
					jsonrpc: "2.0",
					method: "call",
					params: args || {},
				}),
			});
		} catch (network) {
			// fetch rejects only when the request never got an answer: the
			// server is down, the laptop went to sleep, the tab lost its
			// network. Left to propagate it arrives at a catch block as
			// "Failed to fetch", which tells a customer nothing.
			const failure = new Error(translate(
				"Could not reach this Odoo. Check your connection and try again."
			));
			failure.offline = true;
			msgprint({ message: failure.message, indicator: "red" });
			throw failure;
		}

		let body = null;
		try {
			body = await response.json();
		} catch (error) {
			body = null;
		}

		if (body && body.error) {
			// Odoo puts the sentence a person should read in data.message, and
			// the exception class in data.name. Anything else is a bug rather
			// than a refusal, and says so.
			const data = body.error.data || {};
			const detail = data.message || body.error.message ||
				translate("The request could not be completed.");
			const failure = new Error(detail);
			failure.exception = data.name || "";
			// BEFORE the throw, never after and never instead: the caller's
			// catch is entitled to assume the customer has been told.
			msgprint({ message: detail, indicator: "red" });
			throw failure;
		}
		if (!response.ok) {
			// No refusal in the body, so this is the server failing rather
			// than answering. Frappe shows those too.
			const failure = new Error(
				response.status === 403 || response.status === 401
					? translate("Your Odoo session has expired. Please reload the page.")
					: translate("The request could not be completed.")
			);
			failure.status = response.status;
			msgprint({ message: failure.message, indicator: "red" });
			throw failure;
		}
		return body ? body.result : null;
	}

	async function xcall(method, args) {
		const body = await post_json(method_url(method), args);
		return body ? body.message : null;
	}

	function call(options) {
		const spec = options || {};

		// `frappe.client.set_value` is Frappe's generic "write one field" RPC.
		// The chat window uses it for exactly one thing: the language picker in
		// its own header. Anything else reaching here is a call this module has
		// not been taught, and it says so rather than writing something
		// unexpected to the customer's database.
		let request;
		if (spec.method === "frappe.client.set_value") {
			const args = spec.args || {};
			if (args.doctype !== "User" || args.fieldname !== "language") {
				request = Promise.reject(new Error(
					"razyyn_platform: refusing an unmapped set_value on " +
					args.doctype + "." + args.fieldname
				));
			} else {
				request = post_json("/razyyn/api/set_user_language", { language: args.value });
			}
		} else {
			request = post_json(method_url(spec.method), spec.args);
		}

		return request.then(
			(body) => {
				if (spec.callback) spec.callback(body || {});
				return body;
			},
			(error) => {
				// Frappe hands the callback `{exc: ...}` rather than throwing,
				// and the chat window tests `if (!r.exc)` before acting.
				if (spec.callback) spec.callback({ exc: String(error && error.message) });
				if (spec.error) spec.error(error);
				// No second report here: post_json has already shown it. Two
				// notices for one refusal reads as two things going wrong.
				throw error;
			}
		);
	}

	// ─── The event stream ───────────────────────────────────────────────────
	//
	// Frappe's chat receives its live progress over socket.io. Odoo's answer is
	// an EventSource against /razyyn/chat/events, and the two differ in one way
	// that matters: an EventSource REPLAYS ACROSS A RECONNECT. Every event is
	// written to razyyn.agent.chat.event before it is sent, each carries its
	// row id, and the browser returns the last id it saw in `Last-Event-ID`.
	// So the server's deliberate fifty-second recycle, a blinking proxy and a
	// laptop that slept for a moment all cost nothing.
	//
	// `last_id` lives HERE, in memory, which means a reloaded page starts with
	// none and the server starts it at the newest row -- deliberately, so a
	// finished run is not redrawn over a new one. A reload is recovered by
	// rebuilding the transcript and asking for the run state instead. Do not
	// read the replay above as covering it.
	//
	// The connection is closed by the server after a bounded spell and the
	// browser reopens it. That is deliberate: an open SSE holds one Odoo HTTP
	// worker, and a handful of chat windows left open overnight would otherwise
	// take the whole pool and the site would stop answering.
	const realtime = {
		handlers: {},
		source: null,
		last_id: null,

		on: function (event, handler) {
			if (!this.handlers[event]) this.handlers[event] = [];
			this.handlers[event].push(handler);
			this.connect();
		},

		off: function (event, handler) {
			if (!this.handlers[event]) return;
			this.handlers[event] = handler
				? this.handlers[event].filter((known) => known !== handler)
				: [];
		},

		dispatch: function (event, data) {
			(this.handlers[event] || []).forEach((handler) => {
				try {
					handler(data);
				} catch (error) {
					console.error("razyyn_platform: handler for " + event + " failed", error);
				}
			});
		},

		connect: function () {
			if (this.source) return;
			const url = "/razyyn/chat/events" +
				(this.last_id ? "?last_id=" + encodeURIComponent(this.last_id) : "");
			const source = new EventSource(url, { withCredentials: true });
			this.source = source;

			source.addEventListener("razyyn", (packet) => {
				if (packet.lastEventId) this.last_id = packet.lastEventId;
				let envelope;
				try {
					envelope = JSON.parse(packet.data);
				} catch (error) {
					return;
				}
				this.dispatch(envelope.event, envelope.message || {});
			});

			// The server ends the stream on purpose (see above). EventSource
			// reopens it by itself; this only makes sure we are not holding a
			// closed handle and blocking the next `connect`.
			source.addEventListener("error", () => {
				if (source.readyState === EventSource.CLOSED) {
					this.source = null;
					window.setTimeout(() => this.connect(), 1500);
				}
			});
		},
	};

	// ─── A Frappe-shaped page ───────────────────────────────────────────────
	function make_app_page(options) {
		const parent = options.parent;
		const wrapper = element("div", "page-container");
		const head = element("div", "page-head");
		head.appendChild(element("h3", "page-title", escape_html(options.title || "")));
		const content = element("div", "page-content");
		wrapper.appendChild(head);
		wrapper.appendChild(content);
		parent.appendChild(wrapper);

		return {
			wrapper: wrapper,
			main: content,
			// The chat draws its own header and hides this one; the method is
			// here because the chat calls it, and it has nothing to clear.
			clear_primary_action: function () {},
			set_primary_action: function () {},
			set_title: function (title) {
				head.querySelector(".page-title").textContent = title || "";
			},
		};
	}

	// ─── Opening an Odoo record ─────────────────────────────────────────────
	//
	// `frappe.set_route('Form', 'Agent Settings', name)` opens a record in the
	// desk. Inside Odoo the chat runs in a frame within the web client, so the
	// request is passed out to the client action, which opens it with the
	// navigation and breadcrumbs intact. Opened directly (not embedded) there
	// is no client to ask, so the browser goes there itself.
	const ODOO_MODEL_FOR = {
		"Agent Settings": "razyyn.agent.settings",
		"Agent Write Policy": "razyyn.agent.write.policy",
		"Agent Write Log": "razyyn.agent.write.log",
	};

	function set_route(view, doctype, name) {
		const model = ODOO_MODEL_FOR[doctype];
		if (!model) {
			console.warn("razyyn_platform: no Odoo model mapped for '" + doctype + "'");
			return;
		}
		const record = { model: model, res_id: Number(name) || undefined };
		if (window.parent && window.parent !== window) {
			window.parent.postMessage({ razyyn: "open_record", record: record }, window.location.origin);
			return;
		}
		window.location.href = "/odoo/action-base.open_module_tree";
	}

	// ─── Assembly ───────────────────────────────────────────────────────────
	const boot = window.RAZYYN_BOOT || {};

	window.__ = translate;
	window.frappe = window.frappe || {};

	Object.assign(window.frappe, {
		// Frappe creates `frappe.pages['<name>']` when it loads a page, and the
		// page file then hangs `on_page_load` off it. Nothing here should have to
		// know the chat is called "agent-chat", so the container makes an entry
		// the first time one is asked for.
		pages: new Proxy({}, {
			get: function (store, name) {
				if (!(name in store)) store[name] = {};
				return store[name];
			},
		}),
		boot: { lang: boot.lang || "en" },
		session: { user: boot.user || "" },
		csrf_token: boot.csrf_token || "",
		xcall: xcall,
		call: call,
		realtime: realtime,
		show_alert: show_alert,
		msgprint: msgprint,
		hide_msgprint: hide_msgprint,
		confirm: confirm_dialog,
		prompt: prompt_dialog,
		set_route: set_route,
		dom: { freeze: freeze, unfreeze: unfreeze },
		ui: { make_app_page: make_app_page, Dialog: RazyynModal },
		utils: {
			escape_html: escape_html,
			// Frappe strips scripting constructs before anything reaches
			// .html(). The chat window ALSO runs every model-authored string
			// through DOMPurify; this is the second of the two, not the only
			// one, and it stays conservative on purpose.
			xss_sanitise: function (html) {
				if (window.DOMPurify) return window.DOMPurify.sanitize(html);
				return escape_html(html);
			},
		},
	});

	// The chat window posts its uploads straight at a Frappe URL rather than
	// through xcall, because a file is multipart and xcall is JSON. One rule,
	// applied to jQuery's ajax: a Frappe method URL becomes the Odoo route of
	// the same name. Keeping the rule here means the copied file needs no edit.
	if (window.jQuery) {
		const FRAPPE_METHOD_URL = "/api/method/";
		window.jQuery.ajaxPrefilter(function (options) {
			if (!options.url || options.url.indexOf(FRAPPE_METHOD_URL) !== 0) return;
			options.url = method_url(options.url.slice(FRAPPE_METHOD_URL.length));
			// This one is a real form post -- a file cannot travel as JSON --
			// so it is the one call that carries Odoo's CSRF token, and Odoo
			// checks it before the handler runs.
			if (options.data instanceof FormData && boot.csrf_token) {
				options.data.append("csrf_token", boot.csrf_token);
			}
		});
	}

	// Arabic reads right to left, and the chat window inherits the direction
	// from the document rather than setting it per element.
	if ((boot.lang || "").slice(0, 2) === "ar") {
		document.documentElement.setAttribute("dir", "rtl");
	}
})();
