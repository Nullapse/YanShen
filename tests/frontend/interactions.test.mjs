import assert from "node:assert/strict";
import test from "node:test";

import { JSDOM } from "jsdom";

function installDom(html, url = "http://localhost/attempts/7") {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, { url });
  [
    "AbortController",
    "CustomEvent",
    "DOMParser",
    "Element",
    "Event",
    "FormData",
    "HTMLAnchorElement",
    "HTMLFormElement",
    "HTMLElement",
    "InputEvent",
    "MouseEvent",
    "Node",
    "NodeFilter",
  ].forEach((name) => {
    globalThis[name] = dom.window[name];
  });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.localStorage = dom.window.localStorage;
  globalThis.sessionStorage = dom.window.sessionStorage;
  globalThis.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
  window.requestAnimationFrame = (callback) => callback();
  window.scrollTo = () => {};
  return dom;
}

test("alignment applies only to the caret line or selected paragraphs", async () => {
  const dom = installDom(`
    <div class="answer-editor-toolbar" data-editor-toolbar data-editor-target="#answer">
      <button type="button" data-editor-align="left">左</button>
      <button type="button" data-editor-align="center">中</button>
      <button type="button" data-editor-align="right">右</button>
    </div>
    <div id="answer" contenteditable="true" data-text-annotation>第一行\n第二行\n第三行</div>
  `);
  const practice = await import(`../../static/js/practice.js?alignment=${Date.now()}`);
  const annotations = await import(`../../static/js/annotations.js?alignment=${Date.now()}`);
  practice.initializeEditorToolbars(new AbortController().signal);
  const editor = document.querySelector("#answer");
  annotations.renderTextAnnotations(editor);

  const lines = () => Array.from(editor.querySelectorAll(":scope > [data-editor-line]"));
  const selection = window.getSelection();
  const caret = document.createRange();
  caret.setStart(lines()[1].firstChild, 2);
  caret.collapse(true);
  selection.removeAllRanges();
  selection.addRange(caret);
  document.dispatchEvent(new Event("selectionchange"));
  document.querySelector('[data-editor-align="center"]').dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  document.querySelector('[data-editor-align="center"]').click();

  assert.deepEqual(lines().map((line) => line.style.textAlign), ["", "center", ""]);

  const paragraphSelection = document.createRange();
  paragraphSelection.setStart(lines()[0].firstChild, 1);
  paragraphSelection.setEnd(lines()[1].firstChild, 2);
  selection.removeAllRanges();
  selection.addRange(paragraphSelection);
  document.dispatchEvent(new Event("selectionchange"));
  document.querySelector('[data-editor-align="right"]').dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  document.querySelector('[data-editor-align="right"]').click();

  assert.deepEqual(lines().map((line) => line.style.textAlign), ["right", "right", ""]);
  assert.equal(annotations.editableValue(editor), "第一行\n第二行\n第三行");
  dom.window.close();
});

test("an empty answer editor stays empty until the user types", async () => {
  const dom = installDom(`
    <div class="answer-editor-toolbar" data-editor-toolbar data-editor-target="#answer">
      <button type="button" data-editor-align="left">左</button>
      <button type="button" data-editor-align="center">中</button>
      <button type="button" data-editor-align="right">右</button>
    </div>
    <div id="answer" contenteditable="true" data-text-annotation></div>
  `);
  const practice = await import(`../../static/js/practice.js?empty-alignment=${Date.now()}`);
  practice.initializeEditorToolbars(new AbortController().signal);

  assert.equal(document.querySelector("#answer").childNodes.length, 0);
  assert.equal(document.querySelectorAll("#answer > [data-editor-line]").length, 0);
  dom.window.close();
});

test("record filters use regular partial navigation instead of the library list endpoint", async () => {
  const dom = installDom(`
    <main class="main">
      <form class="auto-filter" action="/attempts" method="get">
        <select name="status"><option value="graded" selected>已批改</option></select>
        <input name="q" value="基层治理">
      </form>
    </main>
  `, "http://localhost/attempts");
  const { initializeFilters } = await import(`../../static/js/shell.js?record-filter=${Date.now()}`);
  const navigations = [];
  const form = document.querySelector("form");
  initializeFilters(new AbortController().signal, (url) => navigations.push(url));

  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

  assert.equal(navigations.length, 1);
  assert.equal(navigations[0].pathname, "/attempts");
  assert.equal(navigations[0].searchParams.get("status"), "graded");
  assert.equal(navigations[0].searchParams.get("q"), "基层治理");
  dom.window.close();
});

test("highlighting paragraphs across a blank line keeps exactly one blank paragraph", async () => {
  const dom = installDom(`
    <div class="answer-editor-toolbar" data-editor-toolbar data-editor-target="#answer">
      <button type="button" data-editor-align="left">左</button>
    </div>
    <div id="answer" contenteditable="true" data-text-annotation
      data-annotation-type="answer" data-annotation-id="7" data-highlight-scope="attempt-7">甲\n\n乙文</div>
  `);
  dom.window.Range.prototype.getBoundingClientRect = () => ({
    top: 100,
    bottom: 120,
    left: 50,
    width: 100,
    height: 20,
    right: 150,
  });
  const practice = await import(`../../static/js/practice.js?blank-highlight=${Date.now()}`);
  const annotations = await import(`../../static/js/annotations.js?blank-highlight=${Date.now()}`);
  const controller = new AbortController();
  practice.initializeEditorToolbars(controller.signal);
  annotations.initializeAnnotations(controller.signal);

  const editor = document.querySelector("#answer");
  const lines = editor.querySelectorAll(":scope > [data-editor-line]");
  const range = document.createRange();
  range.setStart(lines[0].firstChild, 0);
  range.setEnd(lines[2].firstChild, lines[2].firstChild.nodeValue.length);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  document.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  await new Promise((resolve) => window.setTimeout(resolve, 5));
  document.querySelector('[data-highlight-color="yellow"]').click();

  const renderedLines = editor.querySelectorAll(":scope > [data-editor-line]");
  assert.equal(annotations.editableValue(editor), "甲\n\n乙文");
  assert.equal(renderedLines.length, 3);
  assert.equal(renderedLines[1].childNodes.length, 0);
  assert.deepEqual(
    Array.from(editor.querySelectorAll(".text-annotation-highlight")).map((mark) => mark.textContent),
    ["甲", "乙文"],
  );
  controller.abort();
  dom.window.close();
});

test("workflow more menu opens on click and its actions remain clickable", async () => {
  const dom = installDom(`
    <div data-workflow-menu>
      <button type="button" data-workflow-menu-toggle aria-expanded="false">更多</button>
      <div data-workflow-menu-popover hidden><button type="button" data-action>操作</button></div>
    </div>
  `);
  const core = await import(`../../static/js/core.js?menu=${Date.now()}`);
  core.initializeWorkflowMenus(new AbortController().signal);
  const menu = document.querySelector("[data-workflow-menu]");
  const toggle = menu.querySelector("[data-workflow-menu-toggle]");
  const popover = menu.querySelector("[data-workflow-menu-popover]");
  let actions = 0;
  popover.querySelector("[data-action]").addEventListener("click", () => { actions += 1; });

  menu.dispatchEvent(new MouseEvent("mouseenter", { bubbles: false }));
  assert.equal(popover.hidden, true);
  toggle.click();
  assert.equal(popover.hidden, false);
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  popover.querySelector("[data-action]").click();
  assert.equal(actions, 1);
  assert.equal(popover.hidden, true);
  dom.window.close();
});

test("silent grading replacement leaves the sidebar navigation untouched", async () => {
  const dom = installDom(`
    <aside class="sidebar"><nav class="nav"><a class="active" href="/attempts">原标签</a></nav></aside>
    <main class="main"><p>旧批改内容</p></main>
  `);
  document.body.dataset.activeSection = "grading";
  const navigation = await import(`../../static/js/navigation.js?silent=${Date.now()}`);
  navigation.configureNavigation(() => {});
  const originalNav = document.querySelector(".sidebar .nav");
  globalThis.fetch = async () => new Response(`<!doctype html><html><head><title>批改完成</title></head>
    <body data-active-section="grading"><aside class="sidebar"><nav class="nav"><a href="/attempts">新标签</a></nav></aside>
    <main class="main"><p>新批改内容</p></main></body></html>`, {
    status: 200,
    headers: { "Content-Type": "text/html" },
  });

  await navigation.navigatePartial(new URL("http://localhost/attempts/7#report-1"), {
    requestInit: { method: "POST" },
    replace: true,
    silent: true,
  });

  assert.equal(document.querySelector(".sidebar .nav"), originalNav);
  assert.equal(originalNav.textContent.trim(), "原标签");
  assert.equal(document.querySelector(".main").textContent.trim(), "新批改内容");
  dom.window.close();
});

test("sidebar sync preserves existing labels while updating active state", async () => {
  const dom = installDom(`
    <nav class="nav">
      <a href="/home">Home</a>
      <div class="nav-cluster is-expanded">
        <a class="nav-primary" href="/papers">Library</a>
        <nav class="nav-submenu"><a class="active" href="/papers">Papers</a><a href="/">All</a></nav>
      </div>
      <a class="nav-settings" href="/settings">Settings</a>
    </nav>
  `, "http://localhost/papers");
  const navigation = await import(`../../static/js/navigation.js?sidebar=${Date.now()}`);
  const currentNav = document.querySelector(".nav");
  const papersLabel = currentNav.querySelector('.nav-submenu a[href="/papers"]');
  const nextDocument = new DOMParser().parseFromString(`
    <nav class="nav">
      <a href="/home">Home</a>
      <div class="nav-cluster is-expanded">
        <a class="nav-primary" href="/papers">Library</a>
        <nav class="nav-submenu"><a href="/papers">Papers</a><a class="active" href="/">All</a></nav>
      </div>
      <a class="nav-settings" href="/settings">Settings</a>
    </nav>
  `, "text/html");

  navigation.syncSidebarNavigation(currentNav, nextDocument.querySelector(".nav"));

  assert.equal(currentNav.querySelector('.nav-submenu a[href="/papers"]'), papersLabel);
  assert.equal(papersLabel.classList.contains("active"), false);
  assert.equal(currentNav.querySelector('.nav-submenu a[href="/"]').classList.contains("active"), true);
  dom.window.close();
});

test("answer editor height follows the requested writing length", async () => {
  const dom = installDom("<main></main>");
  const practice = await import(`../../static/js/practice.js?height=${Date.now()}`);

  assert.equal(practice.answerEditorDefaultHeight("300字以内"), 356);
  assert.equal(practice.answerEditorDefaultHeight("500 字左右"), 556);
  assert.equal(practice.answerEditorDefaultHeight("1000字左右"), 656);
  assert.equal(practice.answerEditorDefaultHeight("未标注"), 356);
  dom.window.close();
});

test("opening or editing the answer does not start an idle timer", async () => {
  const dom = installDom(`
    <div data-practice-timer data-timer-kind="question" data-timer-key="question-9">
      <strong data-timer-display></strong>
      <button type="button" data-timer-toggle>Start</button>
      <button type="button" data-timer-reset>Reset</button>
    </div>
  `);
  const timers = await import(`../../static/js/timers.js?activity=${Date.now()}`);
  const container = document.querySelector("[data-practice-timer]");
  timers.bindPracticeTimer(container);

  assert.equal(timers.readPracticeTimerState("question-9").running, false);
  container.dispatchEvent(new Event("focus"));
  container.dispatchEvent(new InputEvent("beforeinput", { bubbles: true }));
  assert.equal(timers.readPracticeTimerState("question-9").running, false);
  assert.equal("startFromActivity" in container.__practiceTimer, false);
  container.querySelector("[data-timer-toggle]").click();
  assert.equal(timers.readPracticeTimerState("question-9").running, true);

  (window.__gongkaoPageIntervals || []).forEach((timer) => window.clearInterval(timer));
  dom.window.close();
});

test("entering the paper library does not guess a page size before its grid exists", async () => {
  const dom = installDom(`
    <aside class="sidebar"></aside>
    <main class="main" style="width: 1200px"></main>
    <a id="papers-link" href="/papers">题库</a>
  `, "http://localhost/home");
  const navigation = await import(`../../static/js/navigation.js?cross-library=${Date.now()}`);
  const url = navigation.partialNavigationUrl(document.querySelector("#papers-link"));

  assert.equal(url.pathname, "/papers");
  assert.equal(url.searchParams.has("per_page"), false);
  dom.window.close();
});

test("saved paragraph alignment is restored and serialized", async () => {
  const dom = installDom(`
    <div class="answer-editor-toolbar" data-editor-toolbar data-editor-target="#answer">
      <button type="button" data-editor-align="left">左</button>
      <button type="button" data-editor-align="center">中</button>
      <button type="button" data-editor-align="right">右</button>
    </div>
    <div id="answer" contenteditable="true" data-text-annotation
      data-paragraph-alignments='["center","left","right"]'>标题\n正文\n落款</div>
  `);
  const practice = await import(`../../static/js/practice.js?persisted-alignment=${Date.now()}`);
  practice.initializeEditorToolbars(new AbortController().signal);
  const editor = document.querySelector("#answer");

  assert.deepEqual(
    Array.from(editor.querySelectorAll(":scope > [data-editor-line]")).map((line) => line.style.textAlign),
    ["center", "", "right"],
  );
  assert.equal(practice.paragraphAlignmentsJson(editor), '["center","left","right"]');
  dom.window.close();
});
