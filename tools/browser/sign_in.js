// Signing in, from signed out.
//
// WHY THIS SUITE EXISTS
//     acceptance.js checks the chat page as an ALREADY CONNECTED user, and so
//     never once pressed Connect. Two defects lived behind that: a refusal
//     arrived as HTTP 200 with the reason in the body and the chat's catch
//     block only logged it, so a wrong password did nothing visible at all;
//     and signing in refused any account without a matching row here, which
//     locked out everyone whose Razyyn account was made anywhere else.
//
//     It starts by disconnecting, so it is repeatable and so the signed-out
//     state is reached the same way a customer reaches it.
//
//     RAZYYN_EMAIL=you@example.com RAZYYN_PASS=... node sign_in.js
const { open, signIn, sleep } = require("./cdp");

const BASE = process.env.BASE || "http://localhost:8069";
const DB = process.env.DB || "profession";
const SHOTS = process.env.SHOTS || "/tmp";
const TAG = process.env.TAG || "signin";
const acceptance = require("fs").readFileSync(__dirname + "/acceptance.js", "utf8");
const LOGIN = process.env.LOGIN || /LOGIN = process\.env\.LOGIN \|\| "([^"]+)"/.exec(acceptance)[1];
const PW = process.env.PW || /PW = process\.env\.PW \|\| "([^"]+)"/.exec(acceptance)[1];
const EMAIL = process.env.RAZYYN_EMAIL;
const PASS = process.env.RAZYYN_PASS;

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

async function shape(page) {
  return page.evaluate(`
    return {
      card: !!document.querySelector("#agent-auth-form"),
      chat: !!document.querySelector(".agent-chat-layout"),
      message: (function () {
        const m = document.querySelector(".razyyn-modal-backdrop");
        return m ? (m.innerText || "").replace(/\\s+/g, " ").trim() : "";
      })(),
      connected_as: (function () {
        const a = document.querySelector(".agent-email-link strong");
        return a ? (a.innerText || "").trim() : "";
      })(),
    };
  `);
}

async function submit(page, email, password) {
  await page.evaluate(`
    const set = function (sel, v) {
      const el = document.querySelector(sel);
      if (!el) return;
      el.value = v;
      el.dispatchEvent(new Event("input", { bubbles: true }));
    };
    set("#auth-email", ${JSON.stringify(email)});
    set("#auth-password", ${JSON.stringify(password)});
    document.querySelector("#agent-auth-form")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    return true;
  `);
  await sleep(9000);
}

(async () => {
  if (!EMAIL || !PASS) {
    console.error("Set RAZYYN_EMAIL and RAZYYN_PASS to a Razyyn account this suite may sign in as.");
    process.exit(2);
  }
  const page = await open(BASE);
  try {
    await signIn(page, BASE, DB, LOGIN, PW);
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(4000);

    // ── Signed out, the way a customer gets there ───────────────────────
    await page.evaluate(`
      await fetch("/razyyn/api/disconnect_agent", {
        method: "POST", headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {
          agent_email: localStorage.getItem("connected_agent_email") || "" } }),
      });
      localStorage.removeItem("connected_agent_email");
      return true;
    `);
    await page.goto("about:blank");
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(4000);
    let state = await shape(page);
    check("disconnecting really signs you out", state.card && !state.chat,
          JSON.stringify(state));
    await page.screenshot(`${SHOTS}/${TAG}-1-signed-out.png`);
    if (!state.card) throw new Error("no sign-in card to test against");

    // ── A refusal has to be visible ─────────────────────────────────────
    await submit(page, EMAIL, PASS + "-definitely-wrong");
    state = await shape(page);
    check("a wrong password says so instead of doing nothing",
          !!state.message && state.card, JSON.stringify(state).slice(0, 180));
    await page.screenshot(`${SHOTS}/${TAG}-2-refused.png`);

    await page.evaluate(`
      const close = Array.from(document.querySelectorAll(".razyyn-modal-close, .razyyn-btn-primary"))
        .find(Boolean);
      if (close) close.click();
      return true;
    `);
    await sleep(600);

    // ── And the real thing ──────────────────────────────────────────────
    await submit(page, EMAIL, PASS);
    state = await shape(page);
    check("the right password opens the chat", state.chat && !state.card,
          JSON.stringify(state).slice(0, 180));
    check("it says which account is connected",
          state.connected_as.toLowerCase() === EMAIL.toLowerCase(),
          state.connected_as);
    await page.screenshot(`${SHOTS}/${TAG}-3-signed-in.png`);

    // ── And it is still signed in after a reload ────────────────────────
    await page.goto("about:blank");
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(4500);
    state = await shape(page);
    check("a reload does not sign you out again", state.chat && !state.card,
          JSON.stringify(state).slice(0, 180));

    // ── A remembered address that no longer matches ─────────────────────
    //
    // The browser keeps the connected e-mail in localStorage and sends it with
    // every call. Seven routes used it to FIND the connection, so an address
    // that had gone stale -- a different account since, or a record whose
    // column an older version never filled in -- meant "not connected": a
    // sign-in card in front of somebody signed in, and a Send button that did
    // nothing. The address may narrow the search; it must never hide it.
    await page.evaluate(`
      localStorage.setItem("connected_agent_email", "someone-elses-old-address@example.com");
      return true;
    `);
    await page.goto("about:blank");
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(4500);
    state = await shape(page);
    check("a stale remembered address does not sign you out",
          state.chat && !state.card, JSON.stringify(state).slice(0, 180));
    await page.screenshot(`${SHOTS}/${TAG}-4-stale-address.png`);

    const failures = results.filter((r) => !r.ok);
    console.log(`\n${results.length - failures.length}/${results.length} checks passed`);
    process.exitCode = failures.length ? 1 : 0;
  } finally { await page.close(); }
})().catch((e) => { console.error("DRIVER FAILED:", e.message); process.exit(1); });
