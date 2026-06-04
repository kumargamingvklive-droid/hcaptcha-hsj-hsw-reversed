"use strict";

// Node-side runner for the JsRuntime. Boots a jsdom realm, polyfills
// canvas, and serves line-delimited JSON eval requests on stdin/stdout.
//
// Protocol:
//   request:  { "id": <int>, "code": "<js>" }
//   response: { "id": <int>, "ok": true,  "result": <serialized> }
//             { "id": <int>, "ok": false, "error":  "<message>" }
//
// Result serialization:
//   undefined  -> null
//   Uint8Array -> array of byte values (for binary transfer)
//   ArrayBuffer-> array of byte values
//   number / string / boolean / null -> as-is
//   object     -> JSON.parse(JSON.stringify(...))  with fallback to String
//
// stderr is reserved for fatal errors only.

const { JSDOM } = require("jsdom");
const { createCanvas } = require("canvas");
const vm = require("vm");
const readline = require("readline");

// ---------------------------------------------------------------------------
// Build the realm
// ---------------------------------------------------------------------------
const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", {
    url: "https://example.com",
    pretendToBeVisual: true,
    runScripts: "outside-only",
});

const win = dom.window;

// Canvas polyfill — jsdom doesn't ship a canvas implementation
const _canvasMod = require("canvas");
win.HTMLCanvasElement.prototype.getContext = function (type) {
    const cv = createCanvas(this.width || 300, this.height || 150);
    return cv.getContext(type);
};
// Expose CanvasRenderingContext2D and friends — some bundles do
// `instanceof CanvasRenderingContext2D` checks
for (const name of [
    "CanvasRenderingContext2D",
    "CanvasGradient",
    "CanvasPattern",
    "Image",
    "ImageData",
]) {
    if (_canvasMod[name] && win[name] === undefined) {
        try { win[name] = _canvasMod[name]; } catch (_) {}
    }
}

// Make WebAssembly accessible (jsdom doesn't expose it by default)
win.WebAssembly = WebAssembly;

// Expose Node's webcrypto so hCaptcha's crypto.getRandomValues calls work
const nodeCrypto = require("crypto");
if (nodeCrypto.webcrypto) {
    try {
        Object.defineProperty(win, "crypto", {
            value: nodeCrypto.webcrypto,
            writable: true,
            configurable: true,
        });
    } catch (_) { /* jsdom may already expose crypto */ }
}

// Expose atob/btoa (jsdom has these, but explicit doesn't hurt)
win.atob = (s) => Buffer.from(s, "base64").toString("binary");
win.btoa = (s) => Buffer.from(s, "binary").toString("base64");

// globalThis === window inside the realm
win.globalThis = win;
win.self = win;
win.global = win;

// Create the vm context. vm.runInContext executes code as if it were
// running inside the realm, so `var x = ...` lands on the realm's
// global object.
const ctx = vm.createContext(win);

// ---------------------------------------------------------------------------
// Result serialization
// ---------------------------------------------------------------------------
function serialize(value) {
    if (value === undefined) return null;
    if (value === null) return null;
    const t = typeof value;
    if (t === "number" || t === "string" || t === "boolean") return value;
    if (t === "function") return "[Function]";
    if (value instanceof Uint8Array) return Array.from(value);
    if (value instanceof ArrayBuffer) return Array.from(new Uint8Array(value));
    if (Buffer.isBuffer(value)) return Array.from(value);
    if (Array.isArray(value)) return value.map(serialize);
    try {
        return JSON.parse(JSON.stringify(value));
    } catch (_) {
        return String(value);
    }
}

// ---------------------------------------------------------------------------
// I/O loop
// ---------------------------------------------------------------------------
const rl = readline.createInterface({ input: process.stdin });

// Signal ready
process.stdout.write("__READY__\n");

// Queue requests so we handle them in order — important for state
// changes (e.g. `var x = 1; ` then `x` should still see x=1).
let queue = Promise.resolve();

rl.on("line", (line) => {
    queue = queue.then(() => handle(line));
});

async function handle(line) {
    if (!line.trim()) return;
    let req;
    try {
        req = JSON.parse(line);
    } catch (e) {
        process.stdout.write(
            JSON.stringify({ id: 0, ok: false, error: "invalid request" }) + "\n",
        );
        return;
    }
    const id = req.id || 0;
    try {
        let result = vm.runInContext(req.code, ctx, { timeout: 60000 });
        if (result && typeof result.then === "function") {
            result = await result;
        }
        process.stdout.write(
            JSON.stringify({ id, ok: true, result: serialize(result) }) + "\n",
        );
    } catch (e) {
        process.stdout.write(
            JSON.stringify({ id, ok: false, error: String(e && e.message ? e.message : e) }) + "\n",
        );
    }
}

rl.on("close", () => process.exit(0));
