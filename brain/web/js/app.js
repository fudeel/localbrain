import { AgentActions } from "./actions.js";
import { BrainApi } from "./api.js";
import { button, statusPill } from "./components.js";
import { h, icon, replaceChildren } from "./dom.js";
import { Store } from "./store.js";
import { AgentsView } from "./views/agents.js";
import { ConnectionsView } from "./views/connections.js";
import { OverviewView } from "./views/overview.js";
import { TalkView } from "./views/talk.js";

const ROUTES = {
  overview: { label: "Overview", iconName: "home", View: OverviewView },
  agents: { label: "Agents", iconName: "agents", View: AgentsView },
  talk: { label: "Talk", iconName: "talk", View: TalkView },
  connections: { label: "Connections", iconName: "link", View: ConnectionsView },
};

class App {
  #view = null;

  constructor(root) {
    this.root = root;
    this.api = new BrainApi();
    this.store = new Store(this.api);
    this.actions = new AgentActions(this.api, this.store);
    this.api.onUnauthorized = () => this.#askForToken();
    this.main = h("main", { class: "main", id: "main" });
    this.nav = h("nav", { class: "nav", "aria-label": "Main" });
    this.status = h("div", { class: "side-status" });
  }

  async start() {
    this.root.append(this.#shell());
    window.addEventListener("hashchange", () => this.#route());
    this.store.subscribe(() => this.#onStoreChange());

    this.store.health = await this.api.get("/health");
    if (this.store.health.token_required && !this.api.token) {
      this.#askForToken();
      return;
    }
    await this.#begin();
  }

  async #begin() {
    this.#route();
    await this.store.start();
  }

  #shell() {
    return h(
      "div",
      { class: "shell" },
      h(
        "aside",
        { class: "side" },
        h(
          "a",
          { class: "brand", href: "#overview" },
          h("img", { class: "brand-mark", src: "/favicon.svg", alt: "", width: 28, height: 28 }),
          h("span", { class: "brand-name" }, "Local AI Brain"),
        ),
        this.nav,
        h("div", { class: "side-foot" }, this.status, this.#themeToggle()),
      ),
      this.main,
    );
  }

  #themeToggle() {
    const isDark = () =>
      document.documentElement.dataset.theme === "dark" ||
      (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    const toggle = h("button", {
      class: "theme-toggle",
      type: "button",
      onClick: () => {
        const next = isDark() ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        try {
          localStorage.setItem("brain.theme", next);
        } catch (_) {
          // The theme just will not be remembered next time.
        }
        paint();
      },
    });
    const paint = () => {
      const dark = isDark();
      replaceChildren(toggle, icon(dark ? "sun" : "moon", 16), dark ? "Light theme" : "Dark theme");
    };
    paint();
    return toggle;
  }

  #currentRoute() {
    const [name, argument] = location.hash.replace(/^#/, "").split("/");
    return { name: ROUTES[name] ? name : "overview", argument: argument ? decodeURIComponent(argument) : null };
  }

  navigate = (name, argument) => {
    const target = argument ? `#${name}/${encodeURIComponent(argument)}` : `#${name}`;
    if (location.hash === target) this.#route();
    else location.hash = target;
  };

  #route() {
    const { name, argument } = this.#currentRoute();
    const { View, label } = ROUTES[name];
    this.#view?.unmount?.();
    this.main.replaceChildren();
    this.main.dataset.route = name;
    this.#view = new View({ store: this.store, actions: this.actions, navigate: this.navigate });
    this.#view.mount(this.main, argument);
    document.title = `${label} · Local AI Brain`;
    this.main.scrollTop = 0;
    this.#renderNav(name);
  }

  #renderNav(active) {
    replaceChildren(
      this.nav,
      Object.entries(ROUTES).map(([name, route]) =>
        h(
          "a",
          {
            class: `nav-item${name === active ? " nav-item-on" : ""}`,
            href: `#${name}`,
            "aria-current": name === active ? "page" : null,
          },
          icon(route.iconName, 18),
          h("span", {}, route.label),
        ),
      ),
    );
  }

  #onStoreChange() {
    this.#view?.update?.();
    this.#renderStatus();
  }

  #renderStatus() {
    const { runtime, connected } = this.store;
    let pill;
    if (!runtime) pill = statusPill("neutral", "Checking…");
    else if (!connected && this.store.health) pill = statusPill("warning", "Reconnecting…", "warning");
    else if (runtime.reachable) pill = statusPill("good", `Ollama ${runtime.version}`, "checkCircle");
    else pill = statusPill("critical", runtime.installed ? "Ollama stopped" : "Ollama missing", "block");
    replaceChildren(this.status, pill);
  }

  #askForToken() {
    if (document.querySelector(".token-screen")) return;
    const input = h("input", {
      class: "input",
      type: "password",
      placeholder: "API token",
      "aria-label": "API token",
      autocomplete: "off",
      required: true,
    });
    const error = h("p", { class: "token-error" });
    const screen = h(
      "div",
      { class: "token-screen" },
      h(
        "form",
        {
          class: "token-card",
          onSubmit: async (event) => {
            event.preventDefault();
            this.api.token = input.value.trim();
            try {
              await this.api.get("/runtime");
              screen.remove();
              await this.#begin();
            } catch (failure) {
              this.api.token = null;
              error.textContent = failure.status === 401 ? "That token was not accepted." : failure.message;
            }
          },
        },
        h("img", { class: "brand-mark", src: "/favicon.svg", alt: "", width: 40, height: 40 }),
        h("h1", { class: "token-title" }, "This brain is protected"),
        h("p", { class: "page-sub" }, "Enter its API token. On the machine running it, run: brain token"),
        input,
        error,
        button("Continue", { kind: "primary", type: "submit" }),
      ),
    );
    document.body.append(screen);
    input.focus();
  }
}

new App(document.getElementById("app")).start();
