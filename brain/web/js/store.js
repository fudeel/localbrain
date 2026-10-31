// Shared application state. Views subscribe to it, and server events keep it current.

import { debounce } from "./dom.js";

const HARDWARE_POLL_MS = 4000;
const RUNTIME_POLL_MS = 8000;
const MAX_ACTIVITY = 60;
const ACTIVE_STATES = { install: "installing", mount: "mounting", unmount: "unmounting", uninstall: "uninstalling" };

export class Store {
  health = null;
  runtime = null;
  hardware = null;
  agents = [];
  runtimeOnline = true;
  devices = [];
  conversations = [];
  activity = [];
  runtimeJob = null;
  connected = false;
  #listeners = new Set();

  constructor(api) {
    this.api = api;
    this.refreshAgents = debounce(() => this.#loadAgents(), 150);
    this.refreshDevices = debounce(() => this.#loadDevices(), 400);
  }

  subscribe(listener) {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  notify() {
    for (const listener of this.#listeners) listener();
  }

  async start() {
    await Promise.all([
      this.#loadRuntime(),
      this.#loadHardware(),
      this.#loadAgents(),
      this.#loadDevices(),
      this.loadConversations(),
    ]);
    setInterval(() => this.#visible() && this.#loadHardware(), HARDWARE_POLL_MS);
    setInterval(() => this.#visible() && this.#loadRuntime(), RUNTIME_POLL_MS);
    this.api.follow(
      (type, data) => this.#onEvent(type, data),
      (connected) => {
        this.connected = connected;
        if (connected) this.refreshAgents();
        this.notify();
      },
    );
  }

  agent(name) {
    return this.agents.find((agent) => agent.name === name) ?? null;
  }

  get mountedAgents() {
    return this.agents.filter((agent) => agent.state === "mounted");
  }

  get talkableAgents() {
    return this.agents.filter(
      (agent) => agent.installed && !agent.installed.remote && agent.capabilities.includes("completion"),
    );
  }

  async loadConversations() {
    await this.#safely(async () => {
      this.conversations = (await this.api.get("/conversations")).conversations;
    });
    this.notify();
  }

  async reloadRuntime() {
    await this.#loadRuntime();
    await this.#loadAgents();
  }

  #visible() {
    return document.visibilityState === "visible";
  }

  async #safely(work) {
    try {
      await work();
    } catch (error) {
      if (error.status !== 401) console.warn(error);
    }
  }

  async #loadRuntime() {
    await this.#safely(async () => {
      const before = this.runtime?.reachable;
      this.runtime = await this.api.get("/runtime");
      if (before !== undefined && before !== this.runtime.reachable) this.refreshAgents();
    });
    this.notify();
  }

  async #loadHardware() {
    await this.#safely(async () => {
      this.hardware = await this.api.get("/hardware");
    });
    this.notify();
  }

  async #loadAgents() {
    await this.#safely(async () => {
      const overview = await this.api.get("/agents");
      this.agents = overview.agents;
      this.runtimeOnline = overview.runtime_online;
    });
    this.notify();
  }

  async #loadDevices() {
    await this.#safely(async () => {
      this.devices = (await this.api.get("/devices")).devices;
    });
    this.notify();
  }

  #onEvent(type, data) {
    switch (type) {
      case "job.updated":
        this.#onJob(data);
        break;
      case "agent.changed":
        this.refreshAgents();
        break;
      case "runtime.changed":
        this.reloadRuntime();
        break;
      case "device.reading":
        this.refreshDevices();
        this.#log(data, `${data.name} sent ${data.readings.length} reading${data.readings.length === 1 ? "" : "s"}`);
        break;
    }
    this.notify();
  }

  #onJob(job) {
    if (job.kind.startsWith("runtime")) {
      this.runtimeJob = job.is_active === false || job.state !== "running" ? null : job;
      if (job.state === "failed") this.runtimeJobError = job.error;
      else if (job.state === "running") this.runtimeJobError = null;
    } else {
      this.#applyToAgent(job);
    }
    if (job.state !== "running") {
      this.refreshAgents();
      this.#log(job, describeJob(job));
    }
  }

  // Applies progress straight away, so a download bar moves without refetching every agent.
  #applyToAgent(job) {
    const agent = this.agent(job.target);
    if (!agent) return;
    if (job.state === "running") {
      agent.state = ACTIVE_STATES[job.kind] ?? agent.state;
      agent.activity = { job_id: job.id, kind: job.kind, message: job.message, completed: job.completed, total: job.total };
    } else {
      agent.activity = null;
      agent.lastError = job.state === "failed" ? job.error : null;
    }
  }

  #log(data, text) {
    this.activity = [
      { text, at: data.finished_at ?? data.at ?? new Date().toISOString(), failed: data.state === "failed" },
      ...this.activity,
    ].slice(0, MAX_ACTIVITY);
  }
}

const JOB_VERBS = {
  install: "Installed",
  mount: "Mounted",
  unmount: "Unmounted",
  uninstall: "Uninstalled",
  runtime_install: "Installed",
  runtime_start: "Started",
};

function describeJob(job) {
  if (job.state === "failed") return `${job.kind.replace("_", " ")} of ${job.target} failed: ${job.error}`;
  if (job.state === "cancelled") return `Cancelled ${job.kind.replace("_", " ")} of ${job.target}`;
  return `${JOB_VERBS[job.kind] ?? job.kind} ${job.target}`;
}
