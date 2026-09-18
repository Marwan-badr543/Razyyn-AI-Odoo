const { open, sleep } = require("./cdp");
const BASE = process.env.BASE || "http://localhost:8079";
const SHOTS = process.env.SHOTS || "/tmp";
const TAG = process.env.TAG || "dblogin";
const L = process.env.LOGIN || "razyyn_login_check@test.local";
const P = process.env.PW || "razyyn_login_check_pw_1";

(async () => {
  const page = await open(BASE);
  try {
    // Watch every response the page gets, not just the ones we expect.
    await page.goto(`${BASE}/web/database/manager`);
    await sleep(2500);
    await page.evaluate(`
      window.__bad = [];
      const real = window.fetch;
      window.fetch = async function (u, o) {
        const r = await real.apply(this, arguments);
        if (r.status >= 400) {
          let t = ""; try { t = await r.clone().text(); } catch (e) {}
          window.__bad.push({ url: String(u), status: r.status, body: t.slice(0, 400) });
        }
        return r;
      };
      const RealXHR = window.XMLHttpRequest;
      window.XMLHttpRequest = function () {
        const x = new RealXHR();
        x.addEventListener("load", function () {
          if (x.status >= 400) window.__bad.push({ url: x.responseURL, status: x.status,
            body: String(x.responseText || "").slice(0, 400) });
        });
        return x;
      };
      return true;
    `);
    console.log("MANAGER:", JSON.stringify(await page.evaluate(`
      return { title: document.title,
               body: (document.body.innerText || "").replace(/\\s+/g," ").slice(0, 200),
               dbs: Array.from(document.querySelectorAll('a[href*="db="]')).map(a => a.getAttribute("href")) };
    `)));
    await page.screenshot(`${SHOTS}/${TAG}-1-manager.png`);

    // The link the manager itself offers for the database.
    const href = await page.evaluate(`
      const a = document.querySelector('a[href*="db="]');
      return a ? a.getAttribute("href") : "";
    `);
    console.log("CLICKING:", href);
    await page.goto(BASE + href);
    await sleep(3000);
    console.log("AFTER LINK:", JSON.stringify(await page.evaluate(`
      return { url: location.href, title: document.title,
               body: (document.body.innerText || "").replace(/\\s+/g," ").slice(0, 250) };
    `)));
    await page.screenshot(`${SHOTS}/${TAG}-2-login.png`);

    await page.evaluate(`
      const set = (s, v) => { const e = document.querySelector(s); if (e) { e.value = v;
        e.dispatchEvent(new Event("input", { bubbles: true })); } };
      set("input[name=login]", ${JSON.stringify(L)});
      set("input[name=password]", ${JSON.stringify(P)});
      const f = document.querySelector("form.oe_login_form, form");
      if (f) f.submit();
      return true;
    `);
    await sleep(6000);
    console.log("AFTER LOGIN:", JSON.stringify(await page.evaluate(`
      return { url: location.href, title: document.title,
               body: (document.body.innerText || "").replace(/\\s+/g," ").slice(0, 250),
               bad: window.__bad || [] };
    `)));
    await page.screenshot(`${SHOTS}/${TAG}-3-after.png`);
  } finally { await page.close(); }
})().catch((e) => { console.error("DRIVER FAILED:", e.message); process.exit(1); });
