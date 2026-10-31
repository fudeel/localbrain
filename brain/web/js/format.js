export function bytes(value) {
  if (value === null || value === undefined) return "unknown size";
  let amount = value;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (amount < 1024) {
      const digits = unit === "GB" || (unit === "MB" && amount < 10) ? 1 : 0;
      return `${amount.toFixed(digits)} ${unit}`;
    }
    amount /= 1024;
  }
  return `${amount.toFixed(1)} TB`;
}

export function percent(part, whole) {
  if (!whole) return 0;
  return Math.min(100, Math.max(0, (part / whole) * 100));
}

export function ago(iso) {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function clockTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function randomId() {
  // crypto.randomUUID only exists on HTTPS or localhost, and this may run over plain LAN HTTP.
  const values = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(values, (value) => value.toString(16).padStart(2, "0")).join("");
}

const DEPLOYMENTS = {
  bare_metal: "Bare metal",
  virtual_machine: "Virtual machine",
  container: "Container",
  kubernetes: "Kubernetes",
};

export function deploymentLabel(kind) {
  return DEPLOYMENTS[kind] ?? kind;
}

export function shortCpu(model) {
  return model.replace(/\(R\)|\(TM\)|\bCPU\b|@.*$/g, "").replace(/\s+/g, " ").trim();
}
