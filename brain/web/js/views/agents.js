import { button, emptyState, progressBar, showError, tag, verdictBadge } from "../components.js";
import { Slot, h, icon, replaceChildren, storage } from "../dom.js";
import { bytes, percent } from "../format.js";
import { runtimeNotice } from "./setup.js";

const FILTERS = [
  { id: "here", label: "Can run here", test: (agent) => agent.compatibility.available },
  { id: "installed", label: "Installed", test: (agent) => Boolean(agent.installed) },
  { id: "not_here", label: "Not on this machine", test: (agent) => !agent.compatibility.available },
  { id: "all", label: "All", test: () => true },
];

const PURPOSES = [
  { id: "home", label: "Home control" },
  { id: "games", label: "Games and stories" },
  { id: "vision", label: "Seeing images" },
  { id: "coding", label: "Coding" },
  { id: "reasoning", label: "Reasoning" },
  { id: "embeddings", label: "Search and memory" },
];

const CAPABILITY_LABELS = {
  tools: "Tools",
  thinking: "Reasoning",
  vision: "Vision",
  embedding: "Embeddings",
  audio: "Audio",
};

const BUSY_LABELS = {
  installing: "Installing",
  mounting: "Mounting",
  unmounting: "Unmounting",
  uninstalling: "Uninstalling",
};

export class AgentsView {
  title = "Agents";
  #filter = storage.get("agents.filter", "here");
  #purpose = null;
  #query = "";
  #expanded = new Set();
  #rows = new Map();
  #custom = null;
  #customError = null;

  #noticeSlot = new Slot();
  #segmentSlot = new Slot("segments");
  #purposeSlot = new Slot("chips");
  #customSlot = new Slot();

  constructor({ store, actions, navigate }) {
    this.store = store;
    this.actions = actions;
    this.navigate = navigate;
    this.root = h("div", { class: "view" });
    this.list = h("div", { class: "agent-list" });
    this.summary = h("p", { class: "page-sub" });
    this.#segmentSlot.element.setAttribute("role", "group");
    this.#segmentSlot.element.setAttribute("aria-label", "Filter by availability");
    this.#purposeSlot.element.setAttribute("role", "group");
    this.#purposeSlot.element.setAttribute("aria-label", "Filter by purpose");
  }

  mount(container) {
    this.root.append(
      h("header", { class: "page-head" }, h("div", { class: "page-head-title" }, h("h1", {}, "Agents")), this.summary),
      this.#noticeSlot.element,
      h(
        "div",
        { class: "toolbar" },
        h("input", {
          class: "input search",
          type: "search",
          placeholder: "Search agents",
          "aria-label": "Search agents",
          value: this.#query,
          onInput: (event) => {
            this.#query = event.target.value.trim().toLowerCase();
            this.#renderList();
          },
        }),
        this.#segmentSlot.element,
      ),
      this.#purposeSlot.element,
      this.#customForm(),
      this.#customSlot.element,
      this.list,
    );
    container.append(this.root);
    this.update();
  }

  update() {
    const { store } = this;
    this.#noticeSlot.render([store.runtime, store.runtimeJob, store.runtimeJobError], () => runtimeNotice(store));
    this.#renderControls();
    this.#renderList();
    this.#renderCustom();
  }

  #renderControls() {
    const agents = this.store.agents;
    const available = agents.filter((agent) => agent.compatibility.available).length;
    this.summary.textContent = agents.length
      ? `${available} of ${agents.length} can run on this machine.`
      : "Loading…";

    const counts = FILTERS.map((filter) => agents.filter(filter.test).length);
    this.#segmentSlot.render([this.#filter, counts], () =>
      FILTERS.map((filter, index) =>
        h(
          "button",
          {
            class: `segment${filter.id === this.#filter ? " segment-on" : ""}`,
            type: "button",
            "aria-pressed": String(filter.id === this.#filter),
            onClick: () => {
              this.#filter = filter.id;
              storage.set("agents.filter", filter.id);
              this.update();
            },
          },
          filter.label,
          h("span", { class: "segment-count" }, counts[index]),
        ),
      ),
    );
    this.#purposeSlot.render(this.#purpose, () =>
      PURPOSES.map((purpose) =>
        h(
          "button",
          {
            class: `chip${purpose.id === this.#purpose ? " chip-on" : ""}`,
            type: "button",
            "aria-pressed": String(purpose.id === this.#purpose),
            onClick: () => {
              this.#purpose = this.#purpose === purpose.id ? null : purpose.id;
              this.update();
            },
          },
          purpose.label,
        ),
      ),
    );
  }

  #visibleAgents() {
    const filter = FILTERS.find((item) => item.id === this.#filter) ?? FILTERS[0];
    return this.store.agents.filter(
      (agent) =>
        filter.test(agent) &&
        (!this.#purpose || agent.use_cases.includes(this.#purpose)) &&
        (!this.#query || `${agent.name} ${agent.summary}`.toLowerCase().includes(this.#query)),
    );
  }

  // Only rows whose data changed are rebuilt, so a download updating several times a second
  // never disturbs focus, an open detail panel, or the scroll position.
  #renderList() {
    const agents = this.#visibleAgents();
    if (agents.length === 0) {
      this.#rows.clear();
      replaceChildren(
        this.list,
        this.store.agents.length
          ? emptyState("agents", "No agents match", "Try another filter, or look an agent up by name above.")
          : h("div", { class: "loading" }, "Loading agents…"),
      );
      return;
    }
    const next = new Map();
    for (const agent of agents) {
      const fingerprint = `${JSON.stringify(agent)}|${this.#expanded.has(agent.name)}`;
      const previous = this.#rows.get(agent.name);
      const element = previous?.fingerprint === fingerprint ? previous.element : this.#row(agent);
      next.set(agent.name, { fingerprint, element });
    }
    const elements = [...next.values()].map((row) => row.element);
    const current = [...this.list.children];
    if (current.length !== elements.length || current.some((node, index) => node !== elements[index])) {
      this.list.replaceChildren(...elements);
    }
    this.#rows = next;
  }

  #row(agent) {
    const expanded = this.#expanded.has(agent.name);
    const headline = worstFinding(agent);
    return h(
      "article",
      { class: `agent${agent.compatibility.available ? "" : " agent-off"}` },
      h(
        "div",
        { class: "agent-top" },
        h(
          "button",
          {
            class: "agent-info",
            type: "button",
            "aria-expanded": String(expanded),
            onClick: () => {
              if (expanded) this.#expanded.delete(agent.name);
              else this.#expanded.add(agent.name);
              this.#renderList();
              this.#renderCustom();
            },
          },
          h(
            "div",
            { class: "agent-title" },
            h("span", { class: `chevron${expanded ? " chevron-open" : ""}` }, icon("chevron", 14)),
            h("span", { class: "agent-name" }, agent.name),
            stateTag(agent),
          ),
          agent.summary ? h("p", { class: "agent-summary" }, agent.summary) : null,
          h(
            "div",
            { class: "tag-row" },
            agent.capabilities.filter((item) => CAPABILITY_LABELS[item]).map((item) => tag(CAPABILITY_LABELS[item])),
          ),
        ),
        h(
          "div",
          { class: "agent-numbers" },
          h("div", { class: "number" }, h("span", { class: "number-value" }, bytes(agent.size_bytes)), h("span", { class: "number-label" }, "download")),
          h(
            "div",
            { class: "number" },
            h("span", { class: "number-value" }, memoryFigure(agent)),
            h("span", { class: "number-label" }, agent.mounted ? "in memory" : "memory needed"),
          ),
        ),
        h("div", { class: "agent-verdict" }, verdictBadge(agent.compatibility.verdict)),
        h("div", { class: "agent-actions" }, this.#actionsFor(agent)),
      ),
      headline ? h("p", { class: `agent-reason agent-reason-${headline.severity}` }, headline.message) : null,
      agent.lastError ? h("p", { class: "agent-reason agent-reason-blocker" }, agent.lastError) : null,
      agent.activity ? activityBar(agent, this.actions) : null,
      expanded ? this.#details(agent) : null,
    );
  }

  #actionsFor(agent) {
    const { actions } = this;
    const available = agent.compatibility.available;
    switch (agent.state) {
      case "available":
        return button("Install", {
          kind: available ? "primary" : "secondary",
          iconName: "download",
          disabled: !available,
          title: available ? null : "This machine cannot run it. Expand for the reason.",
          onClick: () => actions.install(agent.name),
        });
      case "installed":
        return [
          agent.installed.remote
            ? null
            : button("Mount", { kind: "primary", onClick: () => actions.mount(agent.name) }),
          button("Uninstall", { kind: "ghost", onClick: () => actions.uninstall(agent) }),
        ];
      case "mounted":
        return [
          agent.can_chat ? button("Talk", { kind: "primary", onClick: () => this.navigate("talk", agent.name) }) : null,
          button("Unmount", { onClick: () => actions.unmount(agent.name) }),
        ];
      default:
        return null;
    }
  }

  #details(agent) {
    const installed = agent.installed;
    const facts = [
      ["Parameters", agent.parameter_size],
      ["Quantization", installed?.quantization],
      ["Longest context", installed?.context_length ? `${installed.context_length.toLocaleString()} tokens` : null],
      ["Context in use", agent.mounted?.context_length ? `${agent.mounted.context_length.toLocaleString()} tokens` : null],
      ["Stays mounted", agent.mounted ? (agent.mounted.pinned ? "Until unmounted" : "Temporarily, then unloads") : null],
      ["Source", agent.source === "catalog" ? "Curated catalog" : agent.source === "registry" ? "Ollama registry" : "This machine"],
    ].filter(([, value]) => value);
    const canForce =
      !agent.compatibility.available &&
      !agent.compatibility.findings.some((finding) => finding.rule === "local_only") &&
      agent.state === "available";
    return h(
      "div",
      { class: "agent-details" },
      h(
        "ul",
        { class: "findings" },
        agent.compatibility.findings.map((finding) =>
          h(
            "li",
            { class: `finding finding-${finding.severity}` },
            icon({ info: "check", warning: "warning", blocker: "block" }[finding.severity], 15),
            finding.message,
          ),
        ),
      ),
      h(
        "dl",
        { class: "facts" },
        facts.map(([label, value]) => h("div", { class: "fact" }, h("dt", {}, label), h("dd", {}, value))),
      ),
      canForce
        ? button("Install anyway", {
            kind: "ghost",
            size: "small",
            title: "Ignores the check above. The download or the mount may fail.",
            onClick: () => this.actions.install(agent.name, { force: true }),
          })
        : null,
    );
  }

  #customForm() {
    const input = h("input", {
      class: "input",
      type: "text",
      placeholder: "Any other model, like moondream:v2 or hf.co/user/model:tag",
      "aria-label": "Look up any model by name",
      spellcheck: false,
    });
    return h(
      "form",
      {
        class: "lookup",
        onSubmit: async (event) => {
          event.preventDefault();
          const name = input.value.trim();
          if (!name) return;
          await this.#lookUp(name);
        },
      },
      input,
      button("Check", { type: "submit" }),
    );
  }

  async #lookUp(name) {
    try {
      this.#custom = await this.store.api.post("/agents/resolve", { name });
      this.#customError = null;
    } catch (error) {
      this.#custom = null;
      this.#customError = error;
      if (error.status === 401) showError(error);
    }
    this.#renderCustom();
  }

  #renderCustom() {
    if (this.#customError) {
      this.#customSlot.render(["error", this.#customError.message], () =>
        h("p", { class: "agent-reason agent-reason-blocker" }, this.#customError.message),
      );
      return;
    }
    const agent = this.#custom && (this.store.agent(this.#custom.name) ?? this.#custom);
    this.#customSlot.render([agent, agent && this.#expanded.has(agent.name)], () =>
      agent ? h("div", { class: "agent-list" }, this.#row(agent)) : null,
    );
  }
}

function worstFinding(agent) {
  const order = { blocker: 2, warning: 1, info: 0 };
  const worst = [...agent.compatibility.findings].sort((a, b) => order[b.severity] - order[a.severity])[0];
  return worst && worst.severity !== "info" ? worst : null;
}

function memoryFigure(agent) {
  if (agent.mounted) return bytes(agent.mounted.memory_bytes);
  const estimate = agent.compatibility.estimated_memory_bytes;
  return estimate === null ? "unknown" : `~${bytes(estimate)}`;
}

function stateTag(agent) {
  if (agent.state === "mounted") return tag("Mounted", "tag-live");
  if (agent.state === "installed") return tag("Installed", "tag-on");
  return null;
}

function activityBar(agent, actions) {
  const { activity } = agent;
  const share = activity.total ? `${percent(activity.completed, activity.total).toFixed(0)}%` : "";
  return h(
    "div",
    { class: "activity" },
    progressBar(activity.completed, activity.total),
    h(
      "div",
      { class: "activity-text" },
      h("span", {}, activity.message || BUSY_LABELS[agent.state] || "Working"),
      activity.total ? h("span", {}, `${share} of ${bytes(activity.total)}`) : null,
      agent.state === "installing"
        ? button("Cancel", { kind: "ghost", size: "small", onClick: () => actions.cancel(activity.job_id) })
        : null,
    ),
  );
}
