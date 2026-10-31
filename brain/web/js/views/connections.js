import { codeBlock, confirmDialog, emptyState, iconButton, showError, tag } from "../components.js";
import { Slot, h } from "../dom.js";
import { ago, clockTime } from "../format.js";

const STALE_AFTER_MS = 5 * 60 * 1000;

export class ConnectionsView {
  title = "Connections";
  #example = "sensor";
  #devices = new Slot();
  #examples = new Slot();
  #feed = new Slot();

  constructor({ store }) {
    this.store = store;
    this.root = h("div", { class: "view" });
  }

  mount(container) {
    this.root.append(
      h(
        "header",
        { class: "page-head" },
        h("div", { class: "page-head-title" }, h("h1", {}, "Connections")),
        h("p", { class: "page-sub" }, "Everything that reports to this brain, and how to connect more."),
      ),
      this.#devices.element,
      this.#examples.element,
      this.#feed.element,
    );
    container.append(this.root);
    this.update();
  }

  update() {
    this.#renderDevices();
    this.#renderExamples();
    this.#renderFeed();
  }

  #renderDevices() {
    const { devices } = this.store;
    this.#devices.render(devices, () =>
      h(
        "section",
        { class: "panel" },
        h("h2", { class: "panel-title" }, "Connected devices"),
        devices.length === 0
          ? emptyState(
              "sensor",
              "Nothing is connected yet",
              "A device appears here the first time it sends a reading. Try one of the examples below.",
            )
          : h("div", { class: "device-grid" }, devices.map((device) => this.#device(device))),
      ),
    );
  }

  #device(device) {
    const fresh = Date.now() - new Date(device.last_seen).getTime() < STALE_AFTER_MS;
    return h(
      "article",
      { class: "device" },
      h(
        "div",
        { class: "device-head" },
        h("div", {}, h("div", { class: "device-name" }, device.name), h("div", { class: "row-sub" }, `${device.kind}  ·  ${ago(device.last_seen)}`)),
        h("div", { class: "device-tools" }, fresh ? tag("Live", "tag-live") : tag("Quiet"), iconButton("trash", `Remove ${device.name}`, () => this.#remove(device))),
      ),
      h(
        "div",
        { class: "readings" },
        device.latest.map((reading) =>
          h(
            "div",
            { class: "reading" },
            h("span", { class: "reading-label" }, reading.metric),
            h("span", { class: "reading-value" }, formatValue(reading)),
          ),
        ),
      ),
    );
  }

  async #remove(device) {
    const confirmed = await confirmDialog({
      title: `Remove ${device.name}?`,
      body: "Its history is deleted. If it sends another reading, it will appear again.",
      confirmLabel: "Remove",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await this.store.api.delete(`/devices/${encodeURIComponent(device.id)}`);
      this.store.refreshDevices();
    } catch (error) {
      showError(error);
    }
  }

  #renderExamples() {
    const tabs = [
      ["sensor", "Send a reading"],
      ["ask", "Ask an agent"],
      ["python", "From Python"],
      ["events", "Follow events"],
    ];
    const needsToken = this.store.health?.token_required;
    const agentNames = [this.store.mountedAgents[0]?.name, this.store.talkableAgents[0]?.name];
    this.#examples.render([this.#example, needsToken, agentNames], () =>
      h(
        "section",
        { class: "panel" },
        h("h2", { class: "panel-title" }, "Connect something"),
        h(
          "div",
          { class: "segments", role: "group", "aria-label": "Example" },
          tabs.map(([id, label]) =>
            h(
              "button",
              {
                class: `segment${id === this.#example ? " segment-on" : ""}`,
                type: "button",
                "aria-pressed": String(id === this.#example),
                onClick: () => {
                  this.#example = id;
                  this.#renderExamples();
                },
              },
              label,
            ),
          ),
        ),
        codeBlock(this.#snippet(this.#example, needsToken)),
        h(
          "p",
          { class: "panel-note" },
          needsToken
            ? "Other machines must send the API token. Run `brain token` on this machine to see it."
            : "This brain only listens on this machine, so no token is needed. Start it with --host 0.0.0.0 to open it to your network, and it will require one.",
        ),
      ),
    );
  }

  #snippet(kind, needsToken) {
    const origin = location.origin;
    const auth = needsToken ? `  -H "Authorization: Bearer $BRAIN_TOKEN" \\\n` : "";
    const agent = this.store.mountedAgents.find((item) => item.can_chat)?.name ?? this.store.talkableAgents[0]?.name ?? "qwen3:4b-q4_K_M";
    switch (kind) {
      case "ask":
        return `curl ${origin}/api/v1/agents/${agent}/chat \\\n${auth}  -d '{"message": "Is it warm enough to open a window?", "sensor_context": true, "think": false}'`;
      case "python":
        return `from brain.sdk import BrainClient\n\nbrain = BrainClient("${origin}")\n\nbrain.report("kitchen", temperature=(21.5, "°C"), door_open=False)\nprint(brain.ask("${agent}", "Tell my kids a joke about robots."))`;
      case "events":
        return `curl -N ${origin}/api/v1/events${needsToken ? ' \\\n  -H "Authorization: Bearer $BRAIN_TOKEN"' : ""}`;
      default:
        return `curl ${origin}/api/v1/devices/kitchen/readings \\\n${auth}  -d '{"name": "Kitchen", "readings": [{"metric": "temperature", "value": 21.5, "unit": "°C"}]}'`;
    }
  }

  #renderFeed() {
    const items = this.store.activity.slice(0, 12);
    this.#feed.render(items, () =>
      h(
        "section",
        { class: "panel" },
        h("h2", { class: "panel-title" }, "Live activity"),
        items.length === 0
          ? h("p", { class: "panel-note" }, "Waiting for something to happen.")
          : h(
              "ul",
              { class: "feed" },
              items.map((item) =>
                h("li", { class: "feed-item" }, h("span", {}, item.text), h("time", { class: "feed-time" }, clockTime(item.at))),
              ),
            ),
      ),
    );
  }
}

function formatValue(reading) {
  const value = typeof reading.value === "boolean" ? (reading.value ? "Yes" : "No") : reading.value;
  return reading.unit ? `${value} ${reading.unit}` : String(value);
}
