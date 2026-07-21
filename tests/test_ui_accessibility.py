import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
UI_PATH = Path(__file__).parents[1] / "ui" / "index.html"


def test_operator_ui_uses_targeted_live_region_and_narrow_layout_guards() -> None:
    html = UI_PATH.read_text(encoding="utf-8")

    incident_view = re.search(r'<article id="incident-view"[^>]*>', html)
    assert incident_view is not None
    assert "aria-live" not in incident_view.group(0)
    announcement = re.search(
        r'<div\s+id="incident-announcement"[\s\S]*?</div>',
        html,
    )
    assert announcement is not None
    assert 'class="visually-hidden"' in announcement.group(0)
    assert 'role="status"' in announcement.group(0)
    assert 'aria-live="polite"' in announcement.group(0)
    assert 'aria-atomic="true"' in announcement.group(0)

    mobile_css = re.search(
        r"@media \(max-width: 780px\) \{(?P<rules>[\s\S]*?)"
        r"@media \(prefers-reduced-motion",
        html,
    )
    assert mobile_css is not None
    assert "flex-wrap: wrap" in mobile_css.group("rules")
    assert ".brand-copy span { overflow-wrap: anywhere; }" in mobile_css.group("rules")
    assert "grid-template-columns: minmax(0, 30%) minmax(0, 1fr);" in html
    assert ".structured-list dt {\n      min-width: 0;\n      overflow-wrap: anywhere;" in html
    assert ".structured-list dd {\n      min-width: 0;\n      margin: 0;\n      overflow-wrap: anywhere;" in html
    assert "body {\n      min-width: 320px;" in html
    assert 'window.confirm("Approve this exact remediation plan for execution?")' in html


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_silent_polls_announce_each_incident_delta_once() -> None:
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
    this.textWrites = 0;
    this._textContent = "";
  }

  set textContent(value) {
    this.textWrites += 1;
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

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    headers: { get: (name) => name.toLowerCase() === "content-type" ? "application/json" : null },
    json: async () => payload
  };
}

const event = (seq, type, content) => ({
  seq,
  type,
  content,
  timestamp: `2026-07-21T06:00:${String(seq).padStart(2, "0")}Z`
});
const plan = {
  status: "proposed",
  steps: [{
    seq: 1,
    action: "Restart the isolated demo service",
    tool: "restart_service",
    args: { service: "praxis-demo-target" },
    risk_level: "safe",
    rollback: "Restart the prior isolated instance"
  }]
};
const baseline = {
  id: "inc-announcement",
  title: "Checkout latency",
  state: "AWAITING_APPROVAL",
  severity: "high",
  signal: "latency",
  service: "checkout-service",
  source: "fixture",
  created_at: "2026-07-21T06:00:00Z",
  memory_match: null,
  plan,
  trail: [event(1, "thought", { stage: "classification", severity: "high" })]
};
let serverIncident = baseline;
let approvalPosts = 0;
let confirmResult = false;

async function fakeFetch(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  if (method === "POST" && path === `/incidents/${encodeURIComponent(baseline.id)}/approve`) {
    approvalPosts += 1;
    return jsonResponse(serverIncident);
  }
  if (method !== "GET") {
    throw new Error(`Unexpected mutation in accessibility fixture: ${path}`);
  }
  if (path === "/incidents") {
    return jsonResponse({ incidents: serverIncident ? [serverIncident] : [] });
  }
  if (serverIncident && path === `/incidents/${encodeURIComponent(serverIncident.id)}`) {
    return jsonResponse(serverIncident);
  }
  if (path === "/session") {
    return jsonResponse({ role: "operator" });
  }
  throw new Error(`Unexpected request: ${path}`);
}

const windowObject = {
  location: new URL("https://praxis.test/"),
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {},
  addEventListener() {},
  confirm: () => confirmResult
};
windowObject.history = { replaceState() {} };

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
  assert.equal(
    await read('unlockOperatorView("test-ui-operator-token-0123456789abcdef")'),
    true
  );
  await waitFor(
    () => read("viewState.selectedIncident && viewState.selectedIncident.id") === baseline.id,
    "the initial incident baseline"
  );

  assert.equal(read("elements.incidentAnnouncement.textContent"), "");
  const baselineWrites = {
    announcement: read("elements.incidentAnnouncement.textWrites"),
    status: read("elements.globalStatus.textWrites"),
    connection: read("elements.connectionText.textWrites")
  };

  await read("poll()");
  assert.deepEqual({
    announcement: read("elements.incidentAnnouncement.textWrites"),
    status: read("elements.globalStatus.textWrites"),
    connection: read("elements.connectionText.textWrites")
  }, baselineWrites, "an unchanged silent poll must not mutate any live-region text");

  serverIncident = {
    ...baseline,
    trail: [
      ...baseline.trail,
      event(2, "tool_result", { tool: "service_status", status: "ok" })
    ]
  };
  await read("poll()");
  assert.equal(
    read("elements.incidentAnnouncement.textContent"),
    "New decision-trail event 2: Tool result."
  );
  assert.equal(
    read("elements.incidentAnnouncement.textWrites"),
    baselineWrites.announcement + 1
  );

  await read("poll()");
  assert.equal(
    read("elements.incidentAnnouncement.textWrites"),
    baselineWrites.announcement + 1,
    "the same trail event must not be announced twice"
  );

  serverIncident = {
    ...serverIncident,
    trail: [
      ...serverIncident.trail,
      event(3, "tool_result", { tool: "service_status", status: "ok" })
    ]
  };
  await read("poll()");
  assert.equal(
    read("elements.incidentAnnouncement.textContent"),
    "New decision-trail event 3: Tool result."
  );
  assert.equal(
    read("elements.incidentAnnouncement.textWrites"),
    baselineWrites.announcement + 2,
    "a consecutive event with the same label must still change the live-region text"
  );

  serverIncident = {
    ...serverIncident,
    state: "EXECUTING",
    trail: [
      ...serverIncident.trail,
      event(4, "execution", { status: "attempted", tool: "restart_service" })
    ]
  };
  await read("poll()");
  assert.equal(
    read("elements.incidentAnnouncement.textContent"),
    "Incident Checkout latency is now Executing approved plan. New decision-trail event 4: Execution attempt."
  );
  assert.equal(
    read("elements.incidentAnnouncement.textWrites"),
    baselineWrites.announcement + 3
  );

  await read("poll()");
  assert.equal(
    read("elements.incidentAnnouncement.textWrites"),
    baselineWrites.announcement + 3,
    "the same state and event delta must remain silent on later polls"
  );

  serverIncident = { ...serverIncident, state: "AWAITING_APPROVAL" };
  await read("poll()");
  const approveHandler = elementsById.get("approve-button").listeners.get("click");
  assert.equal(typeof approveHandler, "function");
  confirmResult = false;
  approveHandler();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(approvalPosts, 0, "canceling native confirmation must not POST approval");

  confirmResult = true;
  approveHandler();
  await waitFor(() => approvalPosts === 1, "one confirmed approval POST");
  await waitFor(() => read("viewState.actionInFlight") === false, "approval completion");
  assert.equal(approvalPosts, 1, "one native confirmation must produce one approval POST");

  serverIncident = null;
  await read("poll()");
  assert.equal(read("viewState.selectedId"), null);
  assert.equal(read("viewState.announcementSnapshot"), null);
  assert.equal(
    read("elements.incidentAnnouncement.textContent"),
    "",
    "removing the selected incident must clear stale announcement context"
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
