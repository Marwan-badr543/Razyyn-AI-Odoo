// A small Chrome DevTools Protocol driver: launch headless Chrome, sign in to
// Odoo, drive the page, and report every console error it produced.
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const WebSocket = require("ws");

const PORT = Number(process.env.CDP_PORT || 9223);

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function http(url) {
  const res = await fetch(url);
  return res.json();
}

async function launch() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "razyyn-chrome-"));
  const chrome = spawn("/usr/bin/google-chrome", [
    "--headless=new",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check",
    "--disable-gpu", "--disable-dev-shm-usage",
    "--window-size=1440,900",
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  chrome.stderr.on("data", () => {});

  for (let i = 0; i < 60; i++) {
    try {
      const version = await http(`http://127.0.0.1:${PORT}/json/version`);
      if (version.webSocketDebuggerUrl) return { chrome, profile };
    } catch (e) { /* not up yet */ }
    await sleep(250);
  }
  throw new Error("Chrome did not open a debugging port");
}

class Page {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.console = [];
    this.failures = [];
    this.requests = [];
    ws.on("message", (raw) => this._receive(JSON.parse(raw.toString())));
  }

  _receive(msg) {
    if (msg.id && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
      return;
    }
    if (msg.method === "Runtime.consoleAPICalled") {
      const text = (msg.params.args || [])
        .map((a) => a.value !== undefined ? String(a.value) : (a.description || a.type))
        .join(" ");
      this.console.push({ level: msg.params.type, text });
    }
    if (msg.method === "Runtime.exceptionThrown") {
      const d = msg.params.exceptionDetails;
      this.failures.push(d.exception ? (d.exception.description || d.text) : d.text);
    }
    if (msg.method === "Log.entryAdded") {
      const e = msg.params.entry;
      if (e.level === "error") this.failures.push(`${e.source}: ${e.text} ${e.url || ""}`);
    }
    if (msg.method === "Network.responseReceived") {
      this.requests.push({ url: msg.params.response.url, status: msg.params.response.status });
    }
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`${method} timed out`));
        }
      }, 60000);
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression: `(async () => { ${expression} })()`,
      awaitPromise: true, returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description
        || result.exceptionDetails.text);
    }
    return result.result.value;
  }

  async goto(url) {
    await this.send("Page.navigate", { url });
    await this.waitForLoad();
  }

  async waitForLoad() {
    for (let i = 0; i < 80; i++) {
      const state = await this.evaluate("return document.readyState");
      if (state === "complete") return;
      await sleep(250);
    }
  }

  async screenshot(file, full = false) {
    const shot = await this.send("Page.captureScreenshot",
      full ? { captureBeyondViewport: true } : {});
    fs.writeFileSync(file, Buffer.from(shot.data, "base64"));
    return file;
  }

  errors() {
    const consoleErrors = this.console
      .filter((c) => c.level === "error")
      .map((c) => c.text);
    return [...this.failures, ...consoleErrors];
  }
}

async function open(url) {
  const { chrome, profile } = await launch();
  const target = await http(`http://127.0.0.1:${PORT}/json/new?about:blank`).catch(
    () => http(`http://127.0.0.1:${PORT}/json/list`).then((l) => l.find((t) => t.type === "page"))
  );
  const wsUrl = target.webSocketDebuggerUrl
    || (await http(`http://127.0.0.1:${PORT}/json/list`)).find((t) => t.type === "page").webSocketDebuggerUrl;

  const ws = new WebSocket(wsUrl, { perMessageDeflate: false, maxPayload: 256 * 1024 * 1024 });
  await new Promise((resolve, reject) => { ws.once("open", resolve); ws.once("error", reject); });

  const page = new Page(ws);
  await page.send("Page.enable");
  await page.send("Runtime.enable");
  await page.send("Log.enable");
  await page.send("Network.enable");
  page.close = async () => { ws.close(); chrome.kill("SIGTERM"); await sleep(300);
    fs.rmSync(profile, { recursive: true, force: true }); };
  if (url) await page.goto(url);
  return page;
}

/** Sign in to Odoo in the browser itself, so the session cookie is real. */
async function signIn(page, base, db, login, password) {
  await page.goto(`${base}/web/login`);
  const body = await page.evaluate(`
    const res = await fetch("${base}/web/session/authenticate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ jsonrpc: "2.0", params:
        { db: ${JSON.stringify(db)}, login: ${JSON.stringify(login)},
          password: ${JSON.stringify(password)} } }),
    });
    const body = await res.json();
    return body.result ? body.result.uid : ("error: " + JSON.stringify(body.error && body.error.data));
  `);
  return body;
}

module.exports = { open, signIn, sleep };
