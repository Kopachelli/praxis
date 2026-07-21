import os
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
UI_PATH = Path(__file__).parents[1] / "ui" / "index.html"


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_out_of_order_detail_response_cannot_replace_selected_incident() -> None:
    harness = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync(process.env.PRAXIS_UI_PATH, "utf8");
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, "operator UI script was not found");

class FakeTextNode {
  constructor(value) {
    this.textContent = String(value);
  }
}

class FakeElement {
  constructor(tagName, id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.listeners = new Map();
    this._textContent = "";
  }

  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent || "").join("");
  }

  get lastElementChild() {
    return [...this.children].reverse().find((child) => child instanceof FakeElement) || null;
  }

  append(...values) {
    for (const value of values) {
      this.children.push(
        value instanceof FakeElement || value instanceof FakeTextNode
          ? value
          : new FakeTextNode(value)
      );
    }
  }

  replaceChildren(...values) {
    this.children = [];
    this._textContent = "";
    this.append(...values);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }

  focus() {}
}

const elementsById = new Map();
const document = {
  title: "",
  getElementById(id) {
    if (!elementsById.has(id)) elementsById.set(id, new FakeElement("div", id));
    return elementsById.get(id);
  },
  createElement(tagName) {
    return new FakeElement(tagName);
  },
  createTextNode(value) {
    return new FakeTextNode(value);
  }
};

class FakeHeaders {
  constructor(values = {}) {
    this.values = new Map(Object.entries(values));
  }

  set(name, value) {
    this.values.set(String(name).toLowerCase(), String(value));
  }
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => name.toLowerCase() === "content-type" ? "application/json" : null },
    json: async () => payload
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((promiseResolve) => { resolve = promiseResolve; });
  return { promise, resolve };
}

const plan = {
  steps: [{
    seq: 1,
    action: "Restart the selected demo service",
    tool: "restart_service",
    args: { service: "demo-api" },
    risk_level: "safe",
    rollback: "Restart the prior instance"
  }]
};
const incidentA = {
  id: "inc-a",
  title: "Incident A",
  state: "AWAITING_APPROVAL",
  severity: "high",
  signal: "latency",
  service: "service-a",
  source: "test",
  created_at: "2026-07-21T00:00:00Z",
  memory_match: null,
  plan,
  trail: []
};
const incidentB = {
  ...incidentA,
  id: "inc-b",
  title: "Incident B",
  service: "service-b",
  created_at: "2026-07-21T00:01:00Z"
};
const summaries = [incidentA, incidentB];
const raceA = deferred();
const raceB = deferred();
const requests = [];
const posts = [];
let mode = "bootstrap";

async function fakeFetch(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  requests.push({
    method,
    path,
    authorization: options.headers.values.get("authorization")
  });

  if (method === "POST") {
    posts.push({ path, body: JSON.parse(options.body) });
    return jsonResponse({ ok: true });
  }
  if (path === "/incidents") {
    return jsonResponse({ incidents: summaries });
  }
  if (mode === "race" && path === "/incidents/inc-a") return raceA.promise;
  if (mode === "race" && path === "/incidents/inc-b") return raceB.promise;
  if (path === "/incidents/inc-a") return jsonResponse(incidentA);
  if (path === "/incidents/inc-b") return jsonResponse(incidentB);
  if (path === "/session") {
    return jsonResponse({ role: "operator" });
  }
  throw new Error(`Unexpected request: ${method} ${path}`);
}

const windowObject = {
  location: new URL("https://praxis.test/?incident=inc-a"),
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {},
  addEventListener() {},
  confirm: () => true
};
windowObject.history = {
  replaceState(_state, _title, url) {
    windowObject.location = new URL(String(url));
  }
};

const context = vm.createContext({
  AbortController,
  console,
  document,
  fetch: fakeFetch,
  Headers: FakeHeaders,
  Intl,
  setTimeout,
  clearTimeout,
  URL,
  URLSearchParams,
  window: windowObject
});
vm.runInContext(scriptMatch[1], context, { filename: "ui/index.html" });

function read(expression) {
  return vm.runInContext(expression, context);
}

async function waitFor(predicate, label) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

(async () => {
  assert.equal(requests.length, 0, "the locked UI must not make protected requests");
  assert.equal(
    await read('unlockOperatorView("test-ui-operator-token-0123456789abcdef")'),
    true
  );
  await waitFor(
    () => read("viewState.selectedIncident && viewState.selectedIncident.id") === "inc-a",
    "the initial A render"
  );

  mode = "race";
  const pendingA = read("loadSelectedIncident(false)");
  assert.equal(requests.at(-1).path, "/incidents/inc-a");

  read('selectIncident("inc-b")');
  await waitFor(
    () => requests.some((request) => request.path === "/incidents/inc-b"),
    "the B detail request"
  );

  raceB.resolve(jsonResponse(incidentB));
  await waitFor(
    () => read("viewState.selectedIncident && viewState.selectedIncident.id") === "inc-b",
    "the B detail render"
  );

  raceA.resolve(jsonResponse(incidentA));
  await pendingA;
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(read("viewState.selectedId"), "inc-b");
  assert.equal(read("viewState.selectedIncident.id"), "inc-b");
  assert.equal(read("elements.incidentId.textContent"), "inc-b");
  assert.equal(read("elements.incidentTitle.textContent"), "Incident B");
  assert.equal(windowObject.location.search, "?incident=inc-b");

  const sidebar = read(`JSON.stringify([...elements.incidentList.children].map((item) => {
    const button = item.children[0];
    return [button.dataset.incidentId, button.getAttribute("aria-current")];
  }))`);
  assert.equal(sidebar, JSON.stringify([["inc-a", "false"], ["inc-b", "true"]]));

  mode = "after";
  await read('submitDecision("approve")');
  assert.deepEqual(posts, [{
    path: "/incidents/inc-b/approve",
    body: { decision: "approve" }
  }]);

  read('viewState.selectedIncident = { ...viewState.selectedIncident, id: "inc-a" }');
  await read('submitDecision("approve")');
  assert.equal(posts.length, 1, "mismatched selection must fail closed before POST");
  assert.ok(
    requests.every((request) => request.authorization === "Bearer test-ui-operator-token-0123456789abcdef"),
    "every protected request must carry the in-memory bearer token"
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""

    env = os.environ.copy()
    env["PRAXIS_UI_PATH"] = str(UI_PATH)
    completed = subprocess.run(
        [NODE, "-"],
        input=harness,
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
