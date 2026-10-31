// Renders agent replies. It produces DOM nodes rather than an HTML string, so nothing a model
// writes can ever execute. It also tolerates half-written input, as replies arrive in pieces.

import { button, copyText } from "./components.js";
import { h } from "./dom.js";

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;

function inline(text) {
  const nodes = [];
  let position = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > position) nodes.push(text.slice(position, match.index));
    const token = match[0];
    if (match[1]) nodes.push(h("code", {}, token.slice(1, -1)));
    else if (match[2]) nodes.push(h("strong", {}, token.slice(2, -2)));
    else if (match[3]) nodes.push(h("em", {}, token.slice(1, -1)));
    else {
      const split = token.indexOf("](");
      nodes.push(
        h("a", { href: token.slice(split + 2, -1), target: "_blank", rel: "noopener noreferrer" }, token.slice(1, split)),
      );
    }
    position = match.index + token.length;
  }
  if (position < text.length) nodes.push(text.slice(position));
  return nodes;
}

function codeBlock(language, source) {
  const copy = button("Copy", { kind: "ghost", iconName: "copy", size: "small" });
  copy.addEventListener("click", async () => {
    await copyText(source);
    copy.querySelector("span").textContent = "Copied";
    setTimeout(() => (copy.querySelector("span").textContent = "Copy"), 1500);
  });
  return h(
    "div",
    { class: "code" },
    h("div", { class: "code-bar" }, h("span", { class: "code-language" }, language || "text"), copy),
    h("pre", {}, h("code", {}, source)),
  );
}

function table(rows) {
  const cells = (line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => cell.trim());
  const [head, , ...body] = rows;
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, cells(head).map((cell) => h("th", {}, inline(cell))))),
      h("tbody", {}, body.map((row) => h("tr", {}, cells(row).map((cell) => h("td", {}, inline(cell)))))),
    ),
  );
}

export function renderMarkdown(source) {
  const lines = source.split("\n");
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    const fence = line.match(/^\s*```(\S*)/);
    if (fence) {
      const code = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) code.push(lines[index++]);
      index += 1;
      blocks.push(codeBlock(fence[1], code.join("\n")));
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      blocks.push(h(`h${heading[1].length + 2}`, {}, inline(heading[2])));
      index += 1;
      continue;
    }

    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      blocks.push(h("hr"));
      index += 1;
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[index + 1] ?? "")) {
      const rows = [];
      while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) rows.push(lines[index++]);
      blocks.push(table(rows));
      continue;
    }

    if (/^\s*>/.test(line)) {
      const quoted = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) quoted.push(lines[index++].replace(/^\s*>\s?/, ""));
      blocks.push(h("blockquote", {}, renderMarkdown(quoted.join("\n"))));
      continue;
    }

    const listMatch = line.match(/^\s*([-*+]|\d+\.)\s+/);
    if (listMatch) {
      const ordered = /\d/.test(listMatch[1]);
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*(?:[-*+]|\d+\.)\s+(.*)$/);
        if (item) items.push(item[1]);
        else if (/^\s{2,}\S/.test(lines[index]) && items.length) items[items.length - 1] += ` ${lines[index].trim()}`;
        else break;
        index += 1;
      }
      blocks.push(h(ordered ? "ol" : "ul", {}, items.map((item) => h("li", {}, inline(item)))));
      continue;
    }

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const paragraph = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^\s*(```|#{1,4}\s|>|[-*+]\s|\d+\.\s)/.test(lines[index])
    ) {
      paragraph.push(lines[index++]);
    }
    if (paragraph.length === 0) paragraph.push(lines[index++]);
    blocks.push(h("p", {}, inline(paragraph.join("\n"))));
  }
  return blocks;
}
