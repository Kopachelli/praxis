import os
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
UI_PATH = Path(__file__).parents[1] / "ui" / "index.html"


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_lifecycle_and_trail_copy_matches_server_state() -> None:
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

let serverIncident = null;

async function fakeFetch(path, options = {}) {
  if ((options.method || "GET").toUpperCase() !== "GET") {
    throw new Error(`Unexpected mutation in render-only fixture: ${path}`);
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
  confirm: () => true
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

function render(incident) {
  context.incidentFixture = incident;
  read("viewState.selectedId = incidentFixture.id");
  read("viewState.selectedIncident = incidentFixture");
  read("renderSelectedIncident()");
}

function renderedState() {
  return {
    badge: read("elements.heroBadges.children[0].textContent"),
    description: read("elements.planDescription.textContent"),
    placeholder: read("elements.planPlaceholder.textContent"),
    approvalHidden: read("elements.approvalSection.hidden")
  };
}

function notice(decision, incident) {
  context.incidentFixture = incident;
  return JSON.parse(read(`JSON.stringify(decisionNotice(${JSON.stringify(decision)}, incidentFixture))`));
}

function trackDecision(decision, incident) {
  context.incidentFixture = incident;
  context.decisionFixture = decision;
  read("viewState.selectedId = incidentFixture.id");
  read("viewState.selectedIncident = incidentFixture");
  read("viewState.decisionNotice = { incidentId: incidentFixture.id, decision: decisionFixture }");
  read("refreshDecisionNotice()");
  return {
    message: read("elements.globalStatus.textContent"),
    tone: read("elements.globalStatus.dataset.tone")
  };
}

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
const base = {
  id: "inc-copy",
  title: "Checkout latency",
  state: "AWAITING_APPROVAL",
  severity: "high",
  signal: "latency",
  service: "checkout-service",
  source: "fixture",
  created_at: "2026-07-21T04:00:00Z",
  memory_match: null,
  plan,
  trail: []
};
const event = (seq, type, content) => ({
  seq,
  type,
  content,
  timestamp: `2026-07-21T04:00:${String(seq).padStart(2, "0")}Z`
});

(async () => {
  assert.equal(
    await read('unlockOperatorView("test-ui-operator-token-0123456789abcdef")'),
    true
  );
  await new Promise((resolve) => setTimeout(resolve, 0));

  render(base);
  assert.deepEqual(renderedState(), {
    badge: "Awaiting approval",
    description: "This exact plan remains blocked until explicit human approval.",
    placeholder: "",
    approvalHidden: false
  });

  const executing = { ...base, state: "EXECUTING" };
  render(executing);
  assert.deepEqual(renderedState(), {
    badge: "Executing approved plan",
    description: "Human approval was recorded. Praxis is executing the approved plan and recording each attempt and result.",
    placeholder: "",
    approvalHidden: true
  });
  assert.deepEqual(notice("approve", executing), {
    message: "Approval recorded. The approved plan is executing.",
    tone: "neutral"
  });
  assert.deepEqual(trackDecision("approve", executing), {
    message: "Approval recorded. The approved plan is executing.",
    tone: "neutral"
  });

  const resolved = { ...base, state: "RESOLVED" };
  render(resolved);
  assert.deepEqual(renderedState(), {
    badge: "Resolved",
    description: "The approved plan completed. Review the decision trail for execution evidence.",
    placeholder: "",
    approvalHidden: true
  });
  assert.deepEqual(notice("approve", resolved), {
    message: "Approval recorded. Execution completed successfully.",
    tone: "success"
  });
  serverIncident = resolved;
  await read("poll()");
  assert.equal(read("elements.globalStatus.textContent"), "Approval recorded. Execution completed successfully.");
  assert.equal(read("elements.globalStatus.dataset.tone"), "success");

  const failedExecution = {
    ...base,
    trail: [
      event(1, "execution", { status: "attempted", tool: "restart_service" }),
      event(2, "execution", { status: "failed", note: "Approved remediation stopped" })
    ]
  };
  render(failedExecution);
  assert.deepEqual(renderedState(), {
    badge: "Execution failed: review",
    description: "The last approved execution failed. Review the plan and execution results before approving another attempt.",
    placeholder: "",
    approvalHidden: false
  });
  assert.deepEqual(notice("approve", failedExecution), {
    message: "Approval recorded, but execution failed. Review the execution results before another approval.",
    tone: "error"
  });

  const correction = event(3, "approval", { decision: "reject", note: "Inspect logs first" });
  const regenerating = { ...base, state: "TRIAGED", plan: null, trail: [correction] };
  render(regenerating);
  assert.deepEqual(renderedState(), {
    badge: "Regenerating plan",
    description: "The prior plan was rejected or edited. Qwen is generating a replacement from the operator feedback.",
    placeholder: "Qwen is generating and validating a replacement remediation plan.",
    approvalHidden: true
  });
  assert.deepEqual(notice("reject", regenerating), {
    message: "Rejection recorded. Qwen is generating a replacement plan from your feedback.",
    tone: "neutral"
  });
  assert.deepEqual(trackDecision("reject", regenerating), {
    message: "Rejection recorded. Qwen is generating a replacement plan from your feedback.",
    tone: "neutral"
  });

  const regenerationFailed = {
    ...regenerating,
    trail: [
      correction,
      event(4, "thought", {
        stage: "plan_regeneration",
        status: "failed",
        reason: "generation_failed"
      })
    ]
  };
  render(regenerationFailed);
  assert.deepEqual(renderedState(), {
    badge: "Regeneration failed",
    description: "Plan regeneration failed; no replacement plan is active.",
    placeholder: "Plan regeneration failed. Review the decision trail before trying again.",
    approvalHidden: true
  });
  assert.deepEqual(notice("reject", regenerationFailed), {
    message: "Rejection recorded, but plan regeneration failed. Review the decision trail.",
    tone: "error"
  });
  serverIncident = regenerationFailed;
  await read("poll()");
  assert.equal(read("elements.globalStatus.textContent"), "Rejection recorded, but plan regeneration failed. Review the decision trail.");
  assert.equal(read("elements.globalStatus.dataset.tone"), "error");

  const triageFailed = {
    ...base,
    state: "NEW",
    plan: null,
    trail: [event(1, "thought", {
      stage: "initial_triage",
      status: "failed",
      reason: "triage_failed"
    })]
  };
  render(triageFailed);
  assert.deepEqual(renderedState(), {
    badge: "Triage failed",
    description: "Initial triage failed before the alert could advance.",
    placeholder: "Initial triage failed. Review the decision trail for the fixed failure record.",
    approvalHidden: true
  });

  const unsafeText = '<img src=x onerror="globalThis.compromised=true">';
  const trailFixture = {
    ...base,
    trail: [
      event(1, "thought", { stage: "root_cause_reasoning", hypothesis: unsafeText }),
      event(2, "thought", { stage: "memory_recall", status: "miss" }),
      event(3, "thought", { stage: "server_housekeeping", status: "complete" }),
      event(4, "execution", { status: "attempted", tool: "restart_service" }),
      event(5, "execution", { status: "succeeded", tool: "restart_service" }),
      event(6, "qwen_attempt", {
        provider: "qwencloud",
        model: "qwen3.7-max",
        outcome: "succeeded",
        reason: "completed",
        trace_id: "trace-copy"
      })
    ]
  };
  render(trailFixture);
  const labels = JSON.parse(read(`JSON.stringify(elements.timeline.children.map((item) =>
    item.children[1].children[0].children[0].children[0].textContent
  ))`));
  assert.deepEqual(labels, [
    "Qwen reasoning",
    "Memory recall",
    "Agent event",
    "Execution attempt",
    "Execution result",
    "Qwen call outcome"
  ]);
  assert.ok(read("elements.timeline.textContent").includes(unsafeText));
  assert.equal(read("typeof globalThis.compromised"), "undefined");
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
