// The three things the customer said were missing: streaming, a transcript that
// survives a reload, and being able to take a table out of the chat.
const { open, signIn, sleep } = require("./cdp.js");

const BASE = process.env.BASE || "http://localhost:8069";
const DB = process.env.DB || "profession";
const SHOTS = process.env.SHOTS || "/tmp";
const TAG = process.env.TAG || "o17";
// The harness account, named once in acceptance.js. It was spelled out again
// here and the two could not be overridden together, so a run against a
// different user silently graded the wrong one.
const acceptance = require("fs").readFileSync(__dirname + "/acceptance.js", "utf8");
const LOGIN = process.env.LOGIN || /LOGIN = process\.env\.LOGIN \|\| "([^"]+)"/.exec(acceptance)[1];
const PW = process.env.PW || /PW = process\.env\.PW \|\| "([^"]+)"/.exec(acceptance)[1];
const ASK = process.env.ASK ||
  "List every customer with their country as a markdown table.";

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const page = await open();
  try {
    await signIn(page, BASE, DB, LOGIN, PW);
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(3000);

    // A fresh conversation, then type and send exactly as a person would.
    await page.evaluate(`
      const fresh = document.querySelector(".agent-new-chat-btn, .new-chat-btn");
      if (fresh) fresh.click();
      return true;
    `);
    await sleep(1500);

    const typed = await page.evaluate(`
      const box = document.querySelector("#agent-chat-input") ||
                  document.querySelector(".agent-chat-input textarea") ||
                  document.querySelector("textarea");
      if (!box) return "no composer";
      box.focus();
      box.value = ${JSON.stringify(ASK)};
      box.dispatchEvent(new Event("input", { bubbles: true }));
      const send = document.querySelector("#agent-send-trigger");
      if (!send) return "no send button";
      send.click();
      return "sent";
    `);
    check("the composer accepts a message and sends it", typed === "sent", typed);

    // ── Streaming: progress must appear long before the answer ───────────
    let sawProgress = false, sawSteps = false;
    for (let i = 0; i < 40 && !(sawProgress && sawSteps); i++) {
      await sleep(1500);
      const live = await page.evaluate(`
        const box = document.querySelector(".agent-messages, .agent-chat-messages, #agent-messages") || document.body;
        const text = box.innerText || "";
        return {
          chars: text.length,
          steps: document.querySelectorAll(".thinking-step-item, .thinking-steps-list li").length,
          streaming: !!document.querySelector(".agent-streaming, .agent-typing, .agent-cancel-btn"),
        };
      `);
      if (live.streaming || live.chars > 200) sawProgress = true;
      if (live.steps > 0) sawSteps = true;
    }
    check("the answer streams in rather than appearing at the end", sawProgress, "");
    check("the desk's steps are shown while it works", sawSteps, "");
    await page.screenshot(`${SHOTS}/${TAG}-4-streaming.png`);

    // ── Reload in the middle of the turn, and rejoin it ─────────────────
    //
    // The complaint was "chat not saved if i refresh the page, all chats
    // lost". This checks the harder half of the fix: not only that what was
    // said survives, but that a turn STILL RUNNING lands its answer in the
    // tab that reloaded — which is what the durable event log and the
    // committed-transcript fallback are for.
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(4500);
    const rejoined = await page.evaluate(`
      const text = document.body.innerText || "";
      return {
        has_question: text.indexOf(${JSON.stringify(ASK.slice(0, 30))}) !== -1,
        bubbles: document.querySelectorAll(".agent-msg-bubble, .agent-message").length,
      };
    `);
    check("what was said survives a reload", rejoined.has_question,
          `${rejoined.bubbles} bubbles on screen`);

    let landed = false;
    for (let i = 0; i < 90 && !landed; i++) {
      await sleep(2000);
      landed = await page.evaluate(`
        const text = document.body.innerText || "";
        // The question is echoed back; the answer is anything substantial after it.
        return text.length > ${JSON.stringify(ASK).length + 600};
      `);
    }
    check("a turn still running lands its answer in the reloaded tab", landed, "");

    // ── Taking a table out of the chat ──────────────────────────────────
    //
    // Rendered through the chat's OWN renderer rather than by hoping the model
    // answers with a table this time. Whether it does is the model's decision
    // and it varies; whether a table that arrives can be exported is the
    // product's, and that is what this is for.
    const rendered = await page.evaluate(`
      const chat = window.frappe.pages["agent-chat"].agent_chat_instance;
      if (!chat || !chat.ui_manager) return "no chat instance";
      const markdown = [
        "| Account | Amount |",
        "|---|---|",
        "| Office Rent | 310.00 |",
        "| Bank Suspense | 310.00 |",
      ].join("\\n");
      const host = document.createElement("div");
      host.id = "razyyn-render-probe";
      document.body.appendChild(host);
      chat.ui_manager.append_message(window.jQuery(host), "ai", markdown, false, new Date().toISOString());
      await new Promise(function (r) { setTimeout(r, 900); });
      return {
        table: !!host.querySelector("table"),
        rows: host.querySelectorAll("table tr").length,
        actions: Array.from(host.querySelectorAll(".agent-table-action-btn"))
                   .map(function (b) { return (b.className.match(/btn-table-[a-z]+/) || [""])[0]; })
                   .filter(Boolean),
      };
    `);

    check("a table in an answer is rendered as a table",
          rendered && rendered.table && rendered.rows >= 3,
          JSON.stringify(rendered));
    check("the table offers Excel, PDF, CSV and copy",
          rendered && rendered.actions
          && ["excel", "pdf", "csv", "copy"].every(function (k) {
               return rendered.actions.indexOf("btn-table-" + k) !== -1;
             }),
          rendered && rendered.actions ? rendered.actions.join(", ") : "");

    const excel = await page.evaluate(`
      const btn = document.querySelector("#razyyn-render-probe .agent-table-action-btn.btn-table-excel");
      if (!btn) return "no button";
      btn.click();
      for (let i = 0; i < 40; i++) {
        await new Promise(function (r) { setTimeout(r, 250); });
        if (window.XLSX) return "XLSX loaded";
      }
      return "XLSX never loaded";
    `);
    check("pressing Excel loads the workbook library from this site", excel === "XLSX loaded", excel);

    const pdf = await page.evaluate(`
      const btn = document.querySelector("#razyyn-render-probe .agent-table-action-btn.btn-table-pdf");
      if (!btn) return "no button";
      btn.click();
      for (let i = 0; i < 40; i++) {
        await new Promise(function (r) { setTimeout(r, 250); });
        if (window.jspdf && window.jspdf.jsPDF) return "jsPDF loaded";
      }
      return "jsPDF never loaded";
    `);
    check("pressing PDF loads the PDF library from this site", pdf === "jsPDF loaded", pdf);
    await page.screenshot(`${SHOTS}/${TAG}-5-table.png`);

    const failures = results.filter((r) => !r.ok);
    console.log(`\n${results.length - failures.length}/${results.length} checks passed`);
    const errs = page.errors();
    if (errs.length) console.log("console errors:", errs.slice(0, 3).join(" | ").slice(0, 400));
    process.exitCode = failures.length ? 1 : 0;
  } finally {
    await page.close();
  }
})().catch((e) => { console.error("DRIVER FAILED:", e.message); process.exit(1); });
