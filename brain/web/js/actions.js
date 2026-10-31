// Agent lifecycle actions, shared by every view that offers them.

import { agentPath } from "./api.js";
import { confirmDialog, showError, toast } from "./components.js";
import { bytes } from "./format.js";

export class AgentActions {
  constructor(api, store) {
    this.api = api;
    this.store = store;
  }

  async install(name, { force = false } = {}) {
    await this.#run(() => this.api.post(agentPath(name, "/install"), { force }));
  }

  async mount(name, { force = false } = {}) {
    await this.#run(() => this.api.post(agentPath(name, "/mount"), { force }));
  }

  async unmount(name) {
    await this.#run(() => this.api.post(agentPath(name, "/unmount")));
  }

  async uninstall(agent) {
    const confirmed = await confirmDialog({
      title: `Uninstall ${agent.name}?`,
      body: `This deletes its files and frees ${bytes(agent.size_bytes)}. You can download it again at any time.`,
      confirmLabel: "Uninstall",
      danger: true,
    });
    if (confirmed) await this.#run(() => this.api.delete(agentPath(agent.name)));
  }

  async cancel(jobId) {
    try {
      await this.api.delete(`/jobs/${encodeURIComponent(jobId)}`);
      toast("Cancelling. Downloaded parts are kept, so a later install resumes.");
    } catch (error) {
      showError(error);
    }
  }

  async #run(request) {
    try {
      await request();
      this.store.refreshAgents();
    } catch (error) {
      showError(error);
    }
  }
}
