import { agentPath } from "../api.js";
import { button, confirmDialog, emptyState, iconButton, showError, statusPill } from "../components.js";
import { Slot, h, icon, replaceChildren, storage } from "../dom.js";
import { ago, randomId } from "../format.js";
import { renderMarkdown } from "../markdown.js";

const STARTERS = [
  "Invent a treasure-hunt game for two kids, using things found around the house.",
  "Write a very short bedtime story about a friendly robot who looks after a house.",
  "If the living room is 27 °C at 10pm in summer, what should a smart home do?",
];
const FOLLOW_THRESHOLD_PX = 120;

export class TalkView {
  title = "Talk";
  #agentName = null;
  #conversationId = null;
  #messages = [];
  #abort = null;
  #streaming = false;
  #system = "";
  #think = storage.get("talk.think") === "true";
  #sensors = storage.get("talk.sensors") === "true";
  #showSettings = false;
  #images = [];
  #follow = true;
  #sidebarSlot = new Slot("talk-side");
  #headerSlot = new Slot("talk-head");
  #settingsSlot = new Slot();

  constructor({ store, actions, navigate }) {
    this.store = store;
    this.actions = actions;
    this.navigate = navigate;
    this.root = h("div", { class: "talk" });
    this.thread = h("div", { class: "thread", role: "log", "aria-live": "polite" });
    this.scroller = h(
      "div",
      {
        class: "thread-scroll",
        onScroll: () => {
          const { scrollHeight, scrollTop, clientHeight } = this.scroller;
          this.#follow = scrollHeight - scrollTop - clientHeight < FOLLOW_THRESHOLD_PX;
        },
      },
      this.thread,
    );
    this.composer = h("div", { class: "composer-wrap" });
  }

  mount(container, argument) {
    if (argument) this.#agentName = argument;
    this.root.append(
      this.#sidebarSlot.element,
      h(
        "div",
        { class: "talk-main" },
        this.#headerSlot.element,
        this.#settingsSlot.element,
        this.scroller,
        this.composer,
      ),
    );
    container.append(this.root);
    this.#renderThread();
    this.update();
    this.#renderComposer();
  }

  unmount() {
    this.#abort?.abort();
  }

  update() {
    this.#pickAgent();
    this.#renderSidebar();
    this.#renderHeader();
    this.#renderSettings();
    this.#syncComposer();
  }

  get #agent() {
    return this.store.agent(this.#agentName);
  }

  #pickAgent() {
    if (this.#agent?.installed) return;
    const fallback = this.store.talkableAgents.sort(
      (first, second) => Number(second.state === "mounted") - Number(first.state === "mounted"),
    )[0];
    this.#agentName = fallback?.name ?? null;
  }

  #renderSidebar() {
    const { conversations } = this.store;
    this.#sidebarSlot.render([conversations, this.#conversationId], () => [
      button("New conversation", { iconName: "plus", onClick: () => this.#newConversation() }),
      h(
        "div",
        { class: "conversation-list" },
        conversations.length === 0 ? h("p", { class: "panel-note" }, "Your conversations are saved here, on this machine.") : null,
        conversations.map((conversation) =>
          h(
            "div",
            { class: `conversation${conversation.id === this.#conversationId ? " conversation-on" : ""}` },
            h(
              "button",
              { class: "conversation-open", type: "button", onClick: () => this.#open(conversation.id) },
              h("span", { class: "conversation-title" }, conversation.title),
              h("span", { class: "conversation-meta" }, `${conversation.agent}  ·  ${ago(conversation.updated_at)}`),
            ),
            iconButton("trash", "Delete conversation", () => this.#delete(conversation)),
          ),
        ),
      ),
    ]);
  }

  #renderHeader() {
    const agents = this.store.talkableAgents;
    const agent = this.#agent;
    const fingerprint = [agents.map((item) => item.name), this.#agentName, agent?.state, this.#streaming, this.#showSettings];
    this.#headerSlot.render(fingerprint, () => (agents.length ? this.#headerContent(agents, agent) : null));
  }

  #headerContent(agents, agent) {
    const select = h(
      "select",
      {
        class: "input agent-select",
        "aria-label": "Agent to talk to",
        disabled: this.#streaming,
        onChange: (event) => {
          this.#agentName = event.target.value;
          this.update();
          this.#renderThread();
        },
      },
      agents.map((item) => h("option", { value: item.name, selected: item.name === this.#agentName }, item.name)),
    );
    return [
      select,
      this.#agentStatus(agent),
      h("div", { class: "spacer" }),
      button("Options", {
        kind: this.#showSettings ? "secondary" : "ghost",
        iconName: "sliders",
        onClick: () => {
          this.#showSettings = !this.#showSettings;
          this.#renderHeader();
          this.#renderSettings();
        },
      }),
    ];
  }

  #agentStatus(agent) {
    if (!agent) return null;
    switch (agent.state) {
      case "mounted":
        return statusPill("good", "Mounted", "checkCircle");
      case "mounting":
        return statusPill("neutral", "Mounting…");
      case "installed":
        return button("Mount to talk", { kind: "primary", size: "small", onClick: () => this.actions.mount(agent.name) });
      default:
        return statusPill("neutral", agent.state);
    }
  }

  #renderSettings() {
    const canThink = Boolean(this.#agent?.capabilities.includes("thinking"));
    // The draft instructions are deliberately left out of the key, so typing never rebuilds this.
    this.#settingsSlot.render([this.#showSettings, canThink, this.#conversationId], () =>
      this.#showSettings ? this.#settingsContent(canThink) : null,
    );
  }

  #settingsContent(canThink) {
    return h(
        "div",
        { class: "talk-settings" },
        h(
          "label",
          { class: "field" },
          h("span", { class: "field-label" }, "Instructions for this conversation"),
          h("textarea", {
            class: "input",
            rows: 2,
            placeholder: "For example: You are a patient storyteller for children aged 6 and 9.",
            value: this.#system,
            onInput: (event) => {
              this.#system = event.target.value;
            },
          }),
        ),
        h(
          "div",
          { class: "toggles" },
          this.#toggle(
            "Let the agent reason first",
            canThink
              ? "More careful, and noticeably slower on a CPU."
              : "This agent cannot reason separately.",
            this.#think && canThink,
            !canThink,
            (value) => {
              this.#think = value;
              storage.set("talk.think", String(value));
            },
          ),
          this.#toggle(
            "Share sensor readings",
            "Gives the agent the latest values from connected devices.",
            this.#sensors,
            false,
            (value) => {
              this.#sensors = value;
              storage.set("talk.sensors", String(value));
            },
          ),
        ),
    );
  }

  #toggle(label, hint, checked, disabled, onChange) {
    return h(
      "label",
      { class: `toggle${disabled ? " toggle-off" : ""}` },
      h("input", {
        type: "checkbox",
        checked,
        disabled,
        onChange: (event) => onChange(event.target.checked),
      }),
      h("span", {}, h("span", { class: "toggle-label" }, label), h("span", { class: "toggle-hint" }, hint)),
    );
  }

  #renderThread() {
    if (this.#messages.length > 0) {
      replaceChildren(this.thread, this.#messages.map((message) => this.#messageElement(message)));
      return;
    }
    if (this.store.talkableAgents.length === 0) {
      replaceChildren(
        this.thread,
        emptyState(
          "agents",
          "No agents installed yet",
          "Install one to start talking. Everything runs on this machine.",
          button("Choose an agent", { kind: "primary", onClick: () => this.navigate("agents") }),
        ),
      );
      return;
    }
    replaceChildren(
      this.thread,
      h(
        "div",
        { class: "welcome" },
        h("h1", { class: "welcome-title" }, "What shall we work on?"),
        h("p", { class: "welcome-text" }, "Everything stays on your own hardware."),
        h(
          "div",
          { class: "starters" },
          STARTERS.map((text) =>
            h(
              "button",
              {
                class: "starter",
                type: "button",
                onClick: () => {
                  this.input.value = text;
                  this.#resizeInput();
                  this.input.focus();
                },
              },
              text,
            ),
          ),
        ),
      ),
    );
  }

  #messageElement(message) {
    if (message.role === "user") {
      return h(
        "div",
        { class: "message message-user" },
        message.images?.length
          ? h("div", { class: "message-images" }, message.images.map((src) => h("img", { src: `data:image/*;base64,${src}`, alt: "Attached" })))
          : null,
        h("div", { class: "bubble" }, message.content),
      );
    }
    const element = h("div", { class: "message message-agent" });
    message.element = element;
    this.#paintAgentMessage(message);
    return element;
  }

  #paintAgentMessage(message) {
    const { element } = message;
    if (!element) return;
    const waiting = message.streaming && !message.content && !message.thinking;
    const status = message.error
      ? h("div", { class: "message-error" }, statusPill("critical", message.error, "block"))
      : message.interrupted
        ? h("div", { class: "message-note" }, "Stopped")
        : null;
    replaceChildren(
      element,
      h("div", { class: "message-agent-name" }, message.agent ?? this.#agentName),
      message.thinking ? this.#thinkingBlock(message) : null,
      waiting ? h("div", { class: "typing", "aria-label": "Waiting for the agent" }, h("i"), h("i"), h("i")) : null,
      message.content ? h("div", { class: "prose" }, renderMarkdown(message.content)) : null,
      status,
      message.stats ? statsLine(message.stats) : null,
    );
  }

  #thinkingBlock(message) {
    const open = message.thinkingOpen ?? message.streaming;
    return h(
      "details",
      {
        class: "thinking",
        open,
        onToggle: (event) => {
          message.thinkingOpen = event.target.open;
        },
      },
      h("summary", {}, icon("chevron", 13), message.streaming && !message.content ? "Reasoning…" : "Reasoning"),
      h("div", { class: "thinking-body" }, message.thinking),
    );
  }

  #renderComposer() {
    this.input = h("textarea", {
      class: "composer-input",
      rows: 1,
      placeholder: "Message the agent",
      "aria-label": "Message",
      onInput: () => this.#resizeInput(),
      onKeydown: (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          this.#send();
        }
      },
    });
    this.fileInput = h("input", {
      class: "offscreen",
      type: "file",
      accept: "image/*",
      multiple: true,
      tabIndex: -1,
      onChange: (event) => this.#attach(event.target.files),
    });
    this.attachButton = iconButton("image", "Attach an image", () => this.fileInput.click());
    this.sendButton = iconButton("send", "Send", () => this.#send(), { kind: "primary" });
    this.stopButton = iconButton("stop", "Stop", () => this.#abort?.abort(), { kind: "secondary" });
    this.attachments = h("div", { class: "attachments" });
    this.mountHint = h("p", { class: "composer-hint" });
    replaceChildren(
      this.composer,
      h(
        "div",
        { class: "composer" },
        this.attachments,
        h("div", { class: "composer-row" }, this.attachButton, this.fileInput, this.input, this.sendButton, this.stopButton),
      ),
      this.mountHint,
    );
    this.#syncComposer();
  }

  // The composer is updated in place, so background refreshes never cost the user their draft.
  #syncComposer() {
    if (!this.input) return;
    const agent = this.#agent;
    const ready = agent?.state === "mounted";
    this.input.disabled = !ready && !this.#streaming;
    this.sendButton.disabled = !ready;
    this.sendButton.hidden = this.#streaming;
    this.stopButton.hidden = !this.#streaming;
    this.attachButton.hidden = !agent?.capabilities.includes("vision");
    this.input.placeholder = ready ? `Message ${agent.name}` : agent ? "Mount this agent to talk to it" : "Install an agent first";
    this.mountHint.textContent = ready ? "Enter sends. Shift+Enter starts a new line." : "";
    if (this.store.talkableAgents.length > 0 && this.#messages.length === 0 && this.thread.querySelector(".empty")) {
      this.#renderThread();
    }
  }

  #resizeInput() {
    this.input.style.height = "auto";
    this.input.style.height = `${Math.min(this.input.scrollHeight, 220)}px`;
  }

  async #attach(files) {
    for (const file of files) {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      this.#images.push(encoded);
    }
    this.fileInput.value = "";
    this.#renderAttachments();
  }

  #renderAttachments() {
    replaceChildren(
      this.attachments,
      this.#images.map((image, index) =>
        h(
          "div",
          { class: "attachment" },
          h("img", { src: `data:image/*;base64,${image}`, alt: "Attachment" }),
          iconButton("x", "Remove image", () => {
            this.#images.splice(index, 1);
            this.#renderAttachments();
          }),
        ),
      ),
    );
  }

  #newConversation() {
    this.#abort?.abort();
    this.#conversationId = null;
    this.#messages = [];
    this.#system = "";
    this.#renderThread();
    this.update();
    this.input?.focus();
  }

  async #open(conversationId) {
    this.#abort?.abort();
    try {
      const { conversation, messages } = await this.store.api.get(`/conversations/${encodeURIComponent(conversationId)}`);
      this.#conversationId = conversation.id;
      this.#system = conversation.system ?? "";
      if (this.store.agent(conversation.agent)?.installed) this.#agentName = conversation.agent;
      this.#messages = messages.map((message) => ({ ...message }));
      this.#follow = true;
      this.#renderThread();
      this.update();
      this.#scrollToEnd();
    } catch (error) {
      showError(error);
    }
  }

  async #delete(conversation) {
    const confirmed = await confirmDialog({
      title: "Delete this conversation?",
      body: `"${conversation.title}" will be permanently removed from this machine.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await this.store.api.delete(`/conversations/${encodeURIComponent(conversation.id)}`);
      if (conversation.id === this.#conversationId) this.#newConversation();
      await this.store.loadConversations();
    } catch (error) {
      showError(error);
    }
  }

  async #send() {
    const text = this.input.value.trim();
    const agent = this.#agent;
    if (!text || this.#streaming || agent?.state !== "mounted") return;

    this.#conversationId ??= randomId();
    const images = this.#images;
    this.#images = [];
    this.#renderAttachments();
    this.input.value = "";
    this.#resizeInput();

    const reply = { role: "assistant", agent: agent.name, content: "", thinking: "", streaming: true };
    this.#messages.push({ role: "user", content: text, images }, reply);
    this.#follow = true;
    this.#renderThread();
    this.#scrollToEnd();

    this.#streaming = true;
    this.#abort = new AbortController();
    this.#syncComposer();
    this.#renderHeader();
    let scheduled = false;
    const paint = () => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        this.#paintAgentMessage(reply);
        if (this.#follow) this.#scrollToEnd();
      });
    };

    const body = {
      message: text,
      conversation_id: this.#conversationId,
      stream: true,
      system: this.#system.trim() || null,
      think: this.#think && agent.capabilities.includes("thinking"),
      sensor_context: this.#sensors,
    };
    if (images.length) body.images = images;

    try {
      await this.store.api.ndjson(agentPath(agent.name, "/chat"), body, (event) => {
        switch (event.type) {
          case "thinking":
            reply.thinking += event.text;
            break;
          case "content":
            reply.content += event.text;
            break;
          case "done":
            reply.stats = event.stats;
            break;
          case "error":
            reply.error = event.error?.message ?? "The agent failed to reply.";
            break;
        }
        paint();
      }, this.#abort.signal);
    } catch (error) {
      if (error.name === "AbortError") reply.interrupted = true;
      else reply.error = error.message;
    } finally {
      reply.streaming = false;
      this.#streaming = false;
      this.#abort = null;
      this.#paintAgentMessage(reply);
      if (this.#follow) this.#scrollToEnd();
      this.#syncComposer();
      this.#renderHeader();
      this.store.loadConversations();
      this.input.focus();
    }
  }

  #scrollToEnd() {
    this.scroller.scrollTop = this.scroller.scrollHeight;
  }
}

function statsLine(stats) {
  const parts = [];
  if (stats.tokens_per_second) parts.push(`${stats.tokens_per_second.toFixed(1)} tokens/s`);
  if (stats.completion_tokens) parts.push(`${stats.completion_tokens} tokens`);
  if (stats.total_seconds) parts.push(`${stats.total_seconds.toFixed(1)} s`);
  return parts.length ? h("div", { class: "message-stats" }, parts.join("  ·  ")) : null;
}
