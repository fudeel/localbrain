// Builds DOM nodes directly. Text always becomes text nodes, so model output and device names
// can never turn into markup.

export function h(tag, props, ...children) {
  const element = document.createElement(tag);
  applyProps(element, props);
  append(element, children);
  return element;
}

function applyProps(element, props) {
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") element.className = value;
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (key === "cssVars") {
      for (const [name, css] of Object.entries(value)) element.style.setProperty(name, css);
    } else if (key.startsWith("on")) element.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in element && key !== "list") element[key] = value;
    else element.setAttribute(key, value === true ? "" : value);
  }
}

export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function replaceChildren(parent, ...children) {
  parent.replaceChildren();
  append(parent, children);
}

// A region of the page that only rebuilds when its data changes. Server updates arrive constantly,
// and rebuilding regardless would swallow clicks, steal focus and snap open dropdowns shut.
export class Slot {
  #key = null;

  constructor(className = "slot") {
    this.element = h("div", { class: className });
  }

  render(data, build) {
    const key = JSON.stringify(data);
    if (key === this.#key) return;
    this.#key = key;
    replaceChildren(this.element, build());
  }
}

const ICONS = {
  home: "M3 11.5 12 4l9 7.5M5.5 10v9.5h13V10",
  agents: "M12 3a4 4 0 0 1 4 4v1h1a4 4 0 0 1 0 8h-1v1a4 4 0 0 1-8 0v-1H7a4 4 0 0 1 0-8h1V7a4 4 0 0 1 4-4ZM12 9v6M9 12h6",
  talk: "M4 5.5h16v11H9.5L5 20.5v-4H4z",
  link: "M9.5 14.5l5-5M8 11l-2 2a3.5 3.5 0 0 0 5 5l2-2M16 13l2-2a3.5 3.5 0 0 0-5-5l-2 2",
  check: "M5 12.5l4.5 4.5L19 7.5",
  checkCircle: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM8 12.3l2.8 2.8L16 9.5",
  warning: "M12 4 2.8 20h18.4zM12 10v4.5M12 17.5v.2",
  block: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM5.7 5.7l12.6 12.6",
  help: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM9.5 9.5a2.5 2.5 0 1 1 3.6 2.2c-.7.4-1.1 1-1.1 1.8M12 16.8v.2",
  download: "M12 4v11M7.5 11 12 15.5 16.5 11M5 19.5h14",
  trash: "M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 11v5.5M14 11v5.5",
  plus: "M12 5v14M5 12h14",
  send: "M12 19V5M6 11l6-6 6 6",
  stop: "M7 7h10v10H7z",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4",
  moon: "M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z",
  chevron: "M9 6l6 6-6 6",
  copy: "M9 9h10.5v11H9zM5 15H4.5V4H15v.5",
  sliders: "M4 7h9M17 7h3M4 17h3M11 17h9M15 5v4M9 15v4",
  cpu: "M7 7h10v10H7zM10 3.5V7M14 3.5V7M10 17v3.5M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5",
  chip: "M4 8.5h16v9H4zM8 12.5h3M14 12.5h2M4 11H2.5M4 15H2.5",
  memory: "M3.5 7.5h17v9h-17zM7.5 11v2M12 11v2M16.5 11v2M6 16.5V19M18 16.5V19",
  disk: "M4 6.5C4 5 7.6 4 12 4s8 1 8 2.5v11c0 1.5-3.6 2.5-8 2.5s-8-1-8-2.5zM4 6.5C4 8 7.6 9 12 9s8-1 8-2.5",
  sensor: "M12 12.5v.2M8.5 16a5 5 0 0 1 0-7M15.5 9a5 5 0 0 1 0 7M5.5 19a9 9 0 0 1 0-13M18.5 6a9 9 0 0 1 0 13",
  image: "M4 5.5h16v13H4zM4 15.5l4.5-4 3.5 3 3-2.5 5 4.5M15.5 9.5v.2",
  bolt: "M13 3 5.5 13.5H11l-1 7.5 7.5-10.5H12z",
  x: "M6 6l12 12M18 6 6 18",
  refresh: "M19.5 8.5A8 8 0 0 0 5 9.5M4.5 15.5A8 8 0 0 0 19 14.5M19.5 4.5v4h-4M4.5 19.5v-4h4",
};

export function icon(name, size = 18) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.75");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  const path = document.createElementNS(namespace, "path");
  path.setAttribute("d", ICONS[name] ?? "");
  svg.append(path);
  return svg;
}

export function debounce(callback, milliseconds) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), milliseconds);
  };
}

export const storage = {
  get(key, fallback = null) {
    try {
      const value = localStorage.getItem(`brain.${key}`);
      return value === null ? fallback : value;
    } catch (_) {
      return fallback;
    }
  },
  set(key, value) {
    try {
      if (value === null || value === undefined) localStorage.removeItem(`brain.${key}`);
      else localStorage.setItem(`brain.${key}`, value);
    } catch (_) {
      // Only a convenience is lost. The page keeps working for this session.
    }
  },
};
