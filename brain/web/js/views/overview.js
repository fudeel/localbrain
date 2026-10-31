import { button, emptyState, statTile, statusPill, tag, verdictLabel } from "../components.js";
import { Slot, h, icon } from "../dom.js";
import { ago, bytes, clockTime, deploymentLabel, percent, shortCpu } from "../format.js";
import { runtimeNotice } from "./setup.js";

const PERIPHERAL_LABELS = {
  gpio: "GPIO",
  i2c: "I²C",
  spi: "SPI",
  serial: "Serial",
  camera: "Camera",
  bluetooth: "Bluetooth",
  audio: "Audio",
};

export class OverviewView {
  title = "Overview";

  #slots = {
    header: new Slot(),
    notice: new Slot(),
    tiles: new Slot(),
    mounted: new Slot(),
    readiness: new Slot(),
    machine: new Slot(),
    activity: new Slot(),
  };

  constructor({ store, actions, navigate }) {
    this.store = store;
    this.actions = actions;
    this.navigate = navigate;
    this.root = h("div", { class: "view" });
  }

  mount(container) {
    const slots = this.#slots;
    this.root.append(
      slots.header.element,
      slots.notice.element,
      slots.tiles.element,
      h(
        "div",
        { class: "columns" },
        h("div", { class: "column" }, slots.mounted.element, slots.readiness.element),
        h("div", { class: "column" }, slots.machine.element, slots.activity.element),
      ),
    );
    container.append(this.root);
    this.update();
  }

  update() {
    const { store } = this;
    const { hardware } = store;
    const slots = this.#slots;
    slots.notice.render([store.runtime, store.runtimeJob, store.runtimeJobError], () => runtimeNotice(store));
    if (!hardware) {
      slots.header.render("loading", () => h("div", { class: "loading" }, "Reading this machine…"));
      return;
    }
    const { environment, cpu, memory, disk, gpus, sensors, peripherals } = hardware;
    slots.header.render([environment, cpu.model], () => this.#header(hardware));
    slots.tiles.render([cpu, memory, disk, gpus], () => this.#tiles(hardware));
    slots.mounted.render(store.mountedAgents.map((agent) => [agent.name, agent.mounted, agent.can_chat]), () => this.#mounted());
    slots.readiness.render(store.agents.map((agent) => [agent.compatibility.verdict, Boolean(agent.installed)]), () => this.#readiness());
    slots.machine.render([sensors, peripherals], () => this.#machine(hardware));
    slots.activity.render(store.activity.slice(0, 7), () => this.#activity());
  }

  #header(hardware) {
    const environment = hardware.environment;
    const facts = [
      environment.board,
      shortCpu(hardware.cpu.model),
      environment.operating_system,
      `Linux ${environment.kernel.split("-")[0]}`,
    ].filter(Boolean);
    return h(
      "header",
      { class: "page-head" },
      h(
        "div",
        { class: "page-head-title" },
        h("h1", {}, environment.hostname),
        tag(deploymentLabel(environment.deployment)),
      ),
      h("p", { class: "page-sub" }, facts.join("  ·  ")),
    );
  }

  #tiles(hardware) {
    return h(
      "div",
      { class: "tiles" },
      this.#memoryTile(hardware),
      this.#gpuTile(hardware),
      this.#cpuTile(hardware),
      this.#diskTile(hardware),
    );
  }

  #memoryTile({ memory }) {
    const used = memory.total_bytes - memory.available_bytes;
    return statTile({
      label: "Memory",
      iconName: "memory",
      value: `${percent(used, memory.total_bytes).toFixed(0)}%`,
      unit: "in use",
      meterValue: used,
      meterMax: memory.total_bytes,
      detail: `${bytes(memory.available_bytes)} free of ${bytes(memory.total_bytes)}`,
      note: memory.limited_by_cgroup ? `Container limit. The host has ${bytes(memory.host_total_bytes)}.` : null,
    });
  }

  #gpuTile({ gpus }) {
    const usable = gpus.filter((gpu) => gpu.supported);
    if (usable.length === 0) {
      return statTile({
        label: "GPU memory",
        iconName: "chip",
        value: "None",
        detail: gpus.length ? `${gpus[0].name} is not usable by Ollama` : "Agents run on the CPU here",
        note: gpus[0]?.note,
      });
    }
    const total = usable.reduce((sum, gpu) => sum + gpu.vram_total_bytes, 0);
    const free = usable.reduce((sum, gpu) => sum + (gpu.vram_free_bytes ?? gpu.vram_total_bytes), 0);
    return statTile({
      label: "GPU memory",
      iconName: "chip",
      value: bytes(total),
      meterValue: total - free,
      meterMax: total,
      detail: `${bytes(free)} free`,
      note: usable.map((gpu) => gpu.name).join(", "),
    });
  }

  #cpuTile({ cpu }) {
    const load = cpu.load_average_1m ?? 0;
    const limited = cpu.effective_cores < cpu.logical_cores;
    return statTile({
      label: "CPU",
      iconName: "cpu",
      value: `${cpu.effective_cores}`,
      unit: cpu.effective_cores === 1 ? "core" : "cores",
      meterValue: load,
      meterMax: cpu.effective_cores,
      meterLabel: "CPU load",
      detail: `Load ${load.toFixed(2)}  ·  ${cpu.architecture}`,
      note: limited ? `Limited by the container. The host has ${cpu.logical_cores}.` : null,
    });
  }

  #diskTile({ disk }) {
    const used = disk.total_bytes - disk.free_bytes;
    return statTile({
      label: "Disk for agents",
      iconName: "disk",
      value: bytes(disk.free_bytes),
      unit: "free",
      meterValue: used,
      meterMax: disk.total_bytes,
      detail: `of ${bytes(disk.total_bytes)} at ${disk.path}`,
    });
  }

  #mounted() {
    const mounted = this.store.mountedAgents;
    const body =
      mounted.length === 0
        ? emptyState(
            "agents",
            "No agent is mounted",
            "Mounting loads an agent into memory, so it can answer straight away.",
            button("Choose an agent", { kind: "primary", onClick: () => this.navigate("agents") }),
          )
        : h("div", { class: "rows" }, mounted.map((agent) => this.#mountedRow(agent)));
    return panel("Mounted agents", body);
  }

  #mountedRow(agent) {
    const placement = { gpu: "On GPU", hybrid: "GPU and CPU", cpu: "On CPU" }[agent.compatibility.placement];
    return h(
      "div",
      { class: "row" },
      h(
        "div",
        { class: "row-main" },
        h("div", { class: "agent-name" }, agent.name),
        h("div", { class: "row-sub" }, `${bytes(agent.mounted.memory_bytes)} in memory  ·  ${placement}`),
      ),
      h(
        "div",
        { class: "row-actions" },
        agent.can_chat ? button("Talk", { kind: "primary", size: "small", onClick: () => this.navigate("talk", agent.name) }) : null,
        button("Unmount", { size: "small", onClick: () => this.actions.unmount(agent.name) }),
      ),
    );
  }

  #readiness() {
    const counts = { runs_well: 0, limited: 0, unavailable: 0, unknown: 0 };
    let installed = 0;
    for (const agent of this.store.agents) {
      counts[agent.compatibility.verdict] += 1;
      if (agent.installed) installed += 1;
    }
    const figure = (verdict, iconName, tone) =>
      h(
        "button",
        { class: "figure", type: "button", onClick: () => this.navigate("agents") },
        h("span", { class: "figure-value" }, counts[verdict]),
        h("span", { class: `status status-${tone}` }, icon(iconName, 15), verdictLabel(verdict)),
      );
    return panel(
      "What this machine can run",
      h(
        "div",
        { class: "figures" },
        figure("runs_well", "checkCircle", "good"),
        figure("limited", "warning", "warning"),
        figure("unavailable", "block", "critical"),
      ),
      h("p", { class: "panel-note" }, `${installed} installed. Judged from this machine's memory, GPU, CPU and disk.`),
    );
  }

  #machine(hardware) {
    const groups = new Map();
    for (const item of hardware.peripherals) {
      groups.set(item.kind, (groups.get(item.kind) ?? 0) + 1);
    }
    const sensors = hardware.sensors;
    if (sensors.length === 0 && groups.size === 0) {
      return panel(
        "Sensors and ports",
        emptyState("sensor", "Nothing detected on this machine", "Virtual machines rarely expose any. A Raspberry Pi shows its temperature and GPIO here."),
      );
    }
    return panel(
      "Sensors and ports",
      sensors.length
        ? h(
            "div",
            { class: "readings" },
            sensors.slice(0, 8).map((sensor) =>
              h(
                "div",
                { class: "reading" },
                h("span", { class: "reading-label" }, sensor.label),
                h("span", { class: "reading-value" }, `${sensor.value.toFixed(sensor.unit === "RPM" ? 0 : 1)} ${sensor.unit}`),
              ),
            ),
          )
        : null,
      groups.size
        ? h(
            "div",
            { class: "tag-row" },
            [...groups].map(([kind, count]) => tag(`${PERIPHERAL_LABELS[kind] ?? kind}${count > 1 ? ` × ${count}` : ""}`)),
          )
        : null,
    );
  }

  #activity() {
    const items = this.store.activity.slice(0, 7);
    return panel(
      "Recent activity",
      items.length === 0
        ? h("p", { class: "panel-note" }, "Installs, mounts and sensor reports show up here as they happen.")
        : h(
            "ul",
            { class: "feed" },
            items.map((item) =>
              h(
                "li",
                { class: "feed-item" },
                item.failed ? statusPill("critical", item.text, "block") : h("span", {}, item.text),
                h("time", { class: "feed-time", title: clockTime(item.at) }, ago(item.at)),
              ),
            ),
          ),
    );
  }
}

function panel(title, ...content) {
  return h("section", { class: "panel" }, h("h2", { class: "panel-title" }, title), content);
}
