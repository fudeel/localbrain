// Shown whenever the model runtime is missing or stopped: the plug-and-play path.

import { button, codeBlock, progressBar, showError, statusPill } from "../components.js";
import { h, icon } from "../dom.js";
import { percent } from "../format.js";

export function runtimeNotice(store) {
  const runtime = store.runtime;
  if (!runtime || runtime.reachable) return null;
  const job = store.runtimeJob;
  if (job) return runningCard(job);
  if (!runtime.is_local) return remoteCard(runtime);
  return runtime.installed ? stoppedCard(store) : missingCard(store);
}

function card(title, text, ...content) {
  return h(
    "section",
    { class: "setup" },
    h("div", { class: "setup-icon" }, icon("bolt", 22)),
    h("div", { class: "setup-body" }, h("h2", { class: "setup-title" }, title), h("p", { class: "setup-text" }, text), content),
  );
}

function runningCard(job) {
  const share = job.total ? `${percent(job.completed, job.total).toFixed(0)}%` : "";
  return card(
    job.kind === "runtime_install" ? "Installing Ollama" : "Starting Ollama",
    "You can keep using this page. It updates when Ollama is ready.",
    progressBar(job.completed, job.total),
    h("div", { class: "setup-progress" }, h("span", {}, job.message || "Working"), h("span", {}, share)),
  );
}

function remoteCard(runtime) {
  return card(
    "Ollama is not answering",
    `This brain is set to use Ollama at ${runtime.url}. That machine needs to be running it, as it cannot be started from here.`,
  );
}

function stoppedCard(store) {
  return card(
    "Ollama is installed but not running",
    `The program is at ${store.runtime.binary_path}.`,
    failure(store),
    h(
      "div",
      { class: "setup-actions" },
      button("Start Ollama", { kind: "primary", iconName: "bolt", onClick: () => act(store, "/runtime/start") }),
    ),
  );
}

function missingCard(store) {
  const options = store.runtime.installers;
  const best = options.find((option) => option.usable);
  return card(
    "Ollama is not on this machine yet",
    "Ollama is what runs the agents. It takes a single click to set up, and everything stays on this machine.",
    failure(store),
    h(
      "div",
      { class: "installers" },
      options.map((option) =>
        h(
          "div",
          { class: `installer${option.usable ? "" : " installer-off"}` },
          h(
            "div",
            { class: "installer-text" },
            h("div", { class: "installer-label" }, option.label),
            h("div", { class: "installer-reason" }, option.reason),
          ),
          button("Install", {
            kind: option === best ? "primary" : "secondary",
            iconName: "download",
            disabled: !option.usable,
            onClick: () => act(store, "/runtime/install", { method: option.method }),
          }),
        ),
      ),
    ),
    h("p", { class: "setup-manual" }, "Prefer doing it yourself? Run this in a terminal, then come back:"),
    codeBlock(store.runtime.manual_command),
  );
}

function failure(store) {
  return store.runtimeJobError ? h("div", { class: "setup-error" }, statusPill("critical", store.runtimeJobError, "block")) : null;
}

async function act(store, path, body = {}) {
  try {
    const job = await store.api.post(path, body);
    store.runtimeJob = job;
    store.notify();
  } catch (error) {
    showError(error);
  }
}
