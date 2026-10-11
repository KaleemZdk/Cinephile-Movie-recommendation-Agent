// Run with: node --test tests/frontend
import assert from "node:assert/strict";
import { test } from "node:test";

import { renderMarkdown } from "../../frontend/js/markdown.js";

test("escapes HTML so replies cannot inject markup", () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)"> and <script>bad()</script>');
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<script"));
  assert.ok(html.includes("&lt;img"));
});

test("renders bold, italic and inline code", () => {
  assert.equal(renderMarkdown("**Dark** is *great* with `code`"), "<p><strong>Dark</strong> is <em>great</em> with <code>code</code></p>");
});

test("renders numbered and bulleted lists", () => {
  assert.equal(renderMarkdown("1. One\n2. Two"), "<ol><li>One</li><li>Two</li></ol>");
  assert.equal(renderMarkdown("* A\n* B"), "<ul><li>A</li><li>B</li></ul>");
});

test("keeps paragraphs and line breaks", () => {
  assert.equal(renderMarkdown("First\nsecond\n\nThird"), "<p>First<br>second</p><p>Third</p>");
});

test("only turns http(s) links into anchors, with safe attributes", () => {
  const ok = renderMarkdown("[site](https://example.com/a?b=1)");
  assert.ok(ok.includes('href="https://example.com/a?b=1"') && ok.includes('rel="noopener noreferrer"'));
  const bad = renderMarkdown("[x](javascript:alert(1))");
  assert.ok(!bad.includes("<a "));
});

test("quotes inside link text cannot break out of attributes", () => {
  const html = renderMarkdown('[a](https://e.com/"onmouseover="x)');
  assert.ok(!html.includes('onmouseover="x'));
});

test("handles empty and partial streaming input", () => {
  assert.equal(renderMarkdown(""), "");
  assert.equal(renderMarkdown("**unfinished"), "<p>**unfinished</p>");
});
