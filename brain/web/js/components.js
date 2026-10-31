// Small building blocks shared by every view.

import { h, icon } from "./dom.js";
import { percent } from "./format.js";

export function button(label, { kind = "secondary", iconName, onClick, disabled, title, type = "button", size } = {}) {
  return h(
    "button",
    { class: `btn btn-${kind}${size ? ` btn-${size}` : ""}`, type, onClick, disabled, title },
    iconName ? icon(iconName, 16) : null,
    label ? h("span", {}, label) : null,
  );
}

export function iconButton(iconName, label, onClick, { kind = "ghost", disabled } = {}) {
  return h(
    "button",
    { class: `btn btn-${kind} btn-icon`, type: "button", title: label, "aria-label": label, onClick, disabled },
    icon(iconName, 17),
  );
}

// Status always pairs a colour with an icon and words, so colour never carries it alone.
const VERDICTS = {
  runs_well: { icon: "checkCircle", label: "Runs well", tone: "good" },
  limited: { icon: "warning", label: "Runs with limits", tone: "warning" },
  unavailable: { icon: "block", label: "Not on this machine", tone: "critical" },
  unknown: { icon: "help", label: "Size unknown", tone: "neutral" },
};

export function verdictBadge(verdict) {
  const info = VERDICTS[verdict] ?? VERDICTS.unknown;
  return h("span", { class: `status status-${info.tone}` }, icon(info.icon, 15), info.label);
}

export function verdictLabel(verdict) {
  return (VERDICTS[verdict] ?? VERDICTS.unknown).label;
}

export function statusPill(tone, text, iconName) {
  return h("span", { class: `status status-${tone}` }, iconName ? icon(iconName, 15) : h("span", { class: "dot" }), text);
}

export function tag(text, extraClass = "") {
  return h("span", { class: `tag ${extraClass}`.trim() }, text);
}

// A ratio against a limit. The fill carries severity as it approaches the limit.
export function meter(value, { max = 100, label } = {}) {
  const share = percent(value, max);
  const tone = share >= 90 ? "critical" : share >= 75 ? "warning" : "accent";
  return h(
    "div",
    {
      class: `meter meter-${tone}`,
      role: "meter",
      "aria-valuenow": Math.round(share),
      "aria-valuemin": 0,
      "aria-valuemax": 100,
      "aria-label": label,
    },
    h("div", { class: "meter-fill", cssVars: { "--share": `${share}%` } }),
  );
}

export function progressBar(completed, total) {
  const known = Boolean(total);
  return h(
    "div",
    {
      class: `progress${known ? "" : " progress-busy"}`,
      role: "progressbar",
      "aria-valuenow": known ? Math.round(percent(completed, total)) : null,
    },
    h("div", { class: "progress-fill", cssVars: known ? { "--share": `${percent(completed, total)}%` } : {} }),
  );
}

export function statTile({ label, iconName, value, unit, detail, meterValue, meterMax, meterLabel, note }) {
  return h(
    "section",
    { class: "tile" },
    h("div", { class: "tile-label" }, icon(iconName, 16), label),
    h("div", { class: "tile-value" }, value, unit ? h("span", { class: "tile-unit" }, unit) : null),
    meterValue !== undefined ? meter(meterValue, { max: meterMax, label: meterLabel ?? label }) : null,
    detail ? h("div", { class: "tile-detail" }, detail) : null,
    note ? h("div", { class: "tile-note" }, note) : null,
  );
}

export function emptyState(iconName, title, text, action) {
  return h(
    "div",
    { class: "empty" },
    h("div", { class: "empty-icon" }, icon(iconName, 22)),
    h("div", { class: "empty-title" }, title),
    text ? h("p", { class: "empty-text" }, text) : null,
    action ?? null,
  );
}

export function confirmDialog({ title, body, confirmLabel, danger = false }) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { class: "dialog" });
    const close = (result) => {
      dialog.close();
      dialog.remove();
      resolve(result);
    };
    dialog.append(
      h("h2", { class: "dialog-title" }, title),
      h("p", { class: "dialog-body" }, body),
      h(
        "div",
        { class: "dialog-actions" },
        button("Cancel", { onClick: () => close(false) }),
        button(confirmLabel, { kind: danger ? "danger" : "primary", onClick: () => close(true) }),
      ),
    );
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      close(false);
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

let toastRegion = null;

export function toast(message, { tone = "neutral", hint } = {}) {
  if (!toastRegion) {
    toastRegion = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastRegion);
  }
  const iconName = tone === "critical" ? "block" : tone === "good" ? "checkCircle" : null;
  const item = h(
    "div",
    { class: `toast toast-${tone}` },
    iconName ? icon(iconName, 17) : null,
    h("div", {}, h("div", {}, message), hint ? h("div", { class: "toast-hint" }, hint) : null),
  );
  toastRegion.append(item);
  setTimeout(() => item.remove(), tone === "critical" ? 8000 : 4000);
}

export function showError(error) {
  toast(error.message ?? String(error), { tone: "critical", hint: error.hint });
}

export function codeBlock(text) {
  const copyButton = button("Copy", { kind: "ghost", iconName: "copy", size: "small" });
  copyButton.addEventListener("click", async () => {
    await copyText(text);
    copyButton.querySelector("span").textContent = "Copied";
    setTimeout(() => {
      copyButton.querySelector("span").textContent = "Copy";
    }, 1500);
  });
  return h("div", { class: "code" }, copyButton, h("pre", {}, h("code", {}, text)));
}

export async function copyText(text) {
  // The modern clipboard API only exists on HTTPS or localhost, and this may be plain LAN HTTP.
  if (navigator.clipboard) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = h("textarea", { class: "offscreen", value: text });
  document.body.append(area);
  area.select();
  document.execCommand("copy");
  area.remove();
}
