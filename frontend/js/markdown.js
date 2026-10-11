// A small, safe Markdown renderer for assistant replies.
// Everything is HTML-escaped first, then a limited set of syntax is turned into tags.

const escapeHtml = (text) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function inline(text) {
  let t = escapeHtml(text);
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>");
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return t;
}

export function renderMarkdown(source) {
  const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let paragraph = [];
  let list = null; // { tag: "ul" | "ol", items: [] }

  const flushParagraph = () => {
    if (paragraph.length) out.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (list) out.push(`<${list.tag}>${list.items.map((item) => `<li>${inline(item)}</li>`).join("")}</${list.tag}>`);
    list = null;
  };

  for (const line of lines) {
    const bullet = /^\s*[*\-+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const heading = /^\s{0,3}(#{1,4})\s+(.*)$/.exec(line);

    if (bullet || numbered) {
      flushParagraph();
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tag !== tag) {
        flushList();
        list = { tag, items: [] };
      }
      list.items.push((bullet || numbered)[1]);
    } else if (heading) {
      flushParagraph();
      flushList();
      out.push(`<h4>${inline(heading[2])}</h4>`);
    } else if (!line.trim()) {
      flushParagraph();
      flushList();
    } else if (list && /^\s{2,}\S/.test(line)) {
      list.items[list.items.length - 1] += " " + line.trim(); // wrapped list item
    } else {
      flushList();
      paragraph.push(line.trim());
    }
  }
  flushParagraph();
  flushList();
  return out.join("");
}
