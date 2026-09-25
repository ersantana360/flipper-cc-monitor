// Cloudflare Worker relay ("mailbox") between the local bridge and the Flipper.
// Free plan: Workers + Durable Objects (SQLite-backed) both allow 100k requests/day.
//
//   PUT  /state          {status,detail,pending,ask}   <- local bridge pushes; reply {ok, flipper_online}
//   GET  /state?client=flipper[&wait=ms]                <- Flipper polls (holds up to `wait` ms for a change)
//   GET  /decision?answer=allow|deny[&ask=N]            <- Flipper button
//   GET  /decision?take=1                               <- local bridge collects (and clears) the decision
//   GET  /health
// Every request needs the shared token: header X-Token or ?token=.

const FLIPPER_STALE_MS = 6000;
const MAX_WAIT_MS = 3000;

export class Mailbox {
  constructor(state, env) {
    this.env = env;
    this.s = { status: "idle", detail: "", pending: "0", ask: "0" };
    this.version = 1;
    this.decision = null;      // {answer, ask, ts}
    this.lastFlipper = 0;
    this.waiters = [];
  }

  flipperOnline() { return Date.now() - this.lastFlipper < FLIPPER_STALE_MS; }

  bump() {
    this.version++;
    const w = this.waiters; this.waiters = [];
    for (const resolve of w) resolve();
  }

  json(obj, status = 200) {
    return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  }

  async fetch(req) {
    const url = new URL(req.url);
    const p = url.pathname;

    if (p === "/state" && (req.method === "PUT" || req.method === "POST")) {
      let body = {};
      try { body = await req.json(); } catch { body = {}; }
      for (const k of ["status", "detail", "pending", "ask"]) if (k in body) this.s[k] = String(body[k]).slice(0, 80);
      if (this.s.pending !== "1") this.decision = null;  // nothing to answer any more
      this.bump();
      return this.json({ ok: true, flipper_online: this.flipperOnline(), version: this.version });
    }

    if (p === "/state" && req.method === "GET") {
      if (url.searchParams.get("client") === "flipper") this.lastFlipper = Date.now();
      const wait = Math.min(Number(url.searchParams.get("wait") || 0), MAX_WAIT_MS);
      if (wait > 0) {
        const v = this.version;
        await Promise.race([
          new Promise(resolve => this.waiters.push(resolve)),
          new Promise(resolve => setTimeout(resolve, wait)),
        ]);
        if (this.version === v) { /* timed out: just return the current state */ }
      }
      return this.json({ ...this.s, v: String(this.version) });
    }

    if (p === "/decision" && req.method === "GET") {
      if (url.searchParams.get("take") === "1") {
        const d = this.decision; this.decision = null;
        return this.json(d || {});
      }
      const answer = (url.searchParams.get("answer") || "").toLowerCase();
      this.lastFlipper = Date.now();
      if (answer !== "allow" && answer !== "deny") return this.json({ ok: false, error: "answer must be allow or deny" }, 400);
      if (this.s.pending !== "1") return this.json({ ok: false, error: "nothing pending" });
      this.decision = { answer, ask: url.searchParams.get("ask") || this.s.ask, ts: Date.now() };
      this.s.pending = "0";
      this.s.status = answer === "allow" ? "allowed" : "denied";
      this.bump();
      return this.json({ ok: true });
    }

    if (p === "/health") {
      return this.json({ flipper_online: this.flipperOnline(), state: this.s, decision: this.decision, version: this.version });
    }
    return new Response("not found", { status: 404 });
  }
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const token = req.headers.get("x-token") || url.searchParams.get("token") || "";
    if (!env.CC_TOKEN || token !== env.CC_TOKEN) return new Response("unauthorized", { status: 401 });
    if (url.pathname === "/") return new Response("cc-flipper-relay ok\n");
    const id = env.MAILBOX.idFromName("default");
    return env.MAILBOX.get(id).fetch(req);
  },
};
