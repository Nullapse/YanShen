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

test("a new line inherits centered alignment and keeps the center button active", async () => {
  const dom = installDom(`
    <div class="answer-editor-toolbar" data-editor-toolbar data-editor-target="#answer">
      <button type="button" data-editor-align="left">左</button>
      <button type="button" data-editor-align="center">中</button>
      <button type="button" data-editor-align="right">右</button>
    </div>
    <div id="answer" contenteditable="true" data-text-annotation
      data-paragraph-alignments='["center"]'>居中标题</div>
  `);
  const practice = await import(`../../static/js/practice.js?enter-alignment=${Date.now()}`);
  practice.initializeEditorToolbars(new AbortController().signal);
  const editor = document.querySelector("#answer");
  const firstLine = editor.querySelector(":scope > [data-editor-line]");
  const selection = window.getSelection();
  const caret = document.createRange();
  caret.setStart(firstLine.firstChild, firstLine.firstChild.nodeValue.length);
  caret.collapse(true);
  selection.removeAllRanges();
  selection.addRange(caret);
  document.dispatchEvent(new Event("selectionchange"));

  editor.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  firstLine.append(document.createTextNode("\n"));
  const nextLineCaret = document.createRange();
  nextLineCaret.setStart(firstLine.lastChild, 1);
  nextLineCaret.collapse(true);
  selection.removeAllRanges();
  selection.addRange(nextLineCaret);
  editor.dispatchEvent(new InputEvent("input", {
    bubbles: true,
    inputType: "insertParagraph",
    data: null,
  }));

  assert.equal(practice.paragraphAlignmentsJson(editor), '["center","center"]');
  assert.equal(document.querySelector('[data-editor-align="center"]').getAttribute("aria-pressed"), "true");
  assert.equal(document.querySelector('[data-editor-align="left"]').getAttribute("aria-pressed"), "false");
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

test("provider preset switching syncs base url and quick tag fills model", async () => {
  const dom = installDom(`
    <form class="settings-panel">
      <div class="mode-grid provider-mode-grid" data-provider-group="grading">
        <label class="mode-card"><input type="radio" name="provider_preset" value="official">官方</label>
        <label class="mode-card"><input type="radio" name="provider_preset" value="opencode" checked>Go</label>
        <label class="mode-card"><input type="radio" name="provider_preset" value="custom">自定义</label>
      </div>
      <input name="api_base_url" value="https://opencode.ai/zen/go/v1" data-provider-base-url="grading">
      <input name="model" value="deepseek-v4-flash" data-provider-model="grading">
      <div data-model-tags="grading">
        <button type="button" data-fill-model="deepseek-reasoner">R1</button>
      </div>
    </form>
  `);
  const { initializeShellControls } = await import(`../../static/js/shell.js?provider-preset=${Date.now()}`);
  initializeShellControls(new AbortController().signal);

  const officialRadio = document.querySelector("input[value='official']");
  const urlInput = document.querySelector("[data-provider-base-url='grading']");
  const modelInput = document.querySelector("[data-provider-model='grading']");
  const tagButton = document.querySelector("[data-fill-model='deepseek-reasoner']");

  officialRadio.checked = true;
  officialRadio.dispatchEvent(new Event("change", { bubbles: true }));

  assert.equal(urlInput.value, "https://api.deepseek.com");
  assert.equal(modelInput.value, "deepseek-chat");

  tagButton.click();
  assert.equal(modelInput.value, "deepseek-reasoner");

  dom.window.close();
});

test("master answer popover opens on click and displays diagnosis, score, and material source", async () => {
  const dom = installDom(`
    <div class="report-content">
      <div class="master-benchmark-legend">
        <span class="legend-badge status-hit">完全得分</span>
        <span class="legend-badge status-miss">未得分</span>
      </div>
      <p>
        1. <span class="master-point-span status-hit"
                 tabindex="0" role="button"
                 data-point-status="hit"
                 data-point-status-name="完全得分"
                 data-point-score="+2分 / 满分2分"
                 data-point-eval="你的作答准确命中原词：『深化数字赋能』。核心语义完整。"
                 data-point-source="材料2第3段：『大力推进产业数字化转型』">深化数字赋能</span>
        2. <span class="master-point-span status-miss"
                 tabindex="0" role="button"
                 data-point-status="miss"
                 data-point-status-name="未得分"
                 data-point-score="+0分 / 满分2分"
                 data-point-eval="你的作答未体现该得分点。"
                 data-point-source="材料4第1段：『健全涉企服务快速响应机制』">健全涉企服务响应机制</span>
      </p>
    </div>
  `);
  const { initializeMasterAnswerPopovers } = await import(`../../static/js/grading.js?master-popover=${Date.now()}`);
  initializeMasterAnswerPopovers(new AbortController().signal);

  const hitSpan = document.querySelector(".master-point-span.status-hit");
  const missSpan = document.querySelector(".master-point-span.status-miss");

  // Click hit span
  hitSpan.click();
  let popover = document.querySelector(".point-popover-card");
  assert.ok(popover, "Popover should open on click");
  assert.ok(popover.classList.contains("status-hit"));
  assert.ok(popover.textContent.includes("完全得分"));
  assert.ok(popover.textContent.includes("+2分 / 满分2分"));
  assert.ok(popover.textContent.includes("深化数字赋能"));
  assert.ok(popover.textContent.includes("材料2第3段"));
  assert.ok(hitSpan.classList.contains("active-popover-target"));

  // Click close button
  const closeBtn = popover.querySelector(".point-popover-close");
  closeBtn.click();
  assert.equal(document.querySelector(".point-popover-card"), null);
  assert.ok(!hitSpan.classList.contains("active-popover-target"));

  // Click miss span
  missSpan.click();
  popover = document.querySelector(".point-popover-card");
  assert.ok(popover, "Popover should open for miss span");
  assert.ok(popover.classList.contains("status-miss"));
  assert.ok(popover.textContent.includes("未得分"));
  assert.ok(popover.textContent.includes("健全涉企服务快速响应机制"));

  // Press Escape
  document.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape" }));
  assert.equal(document.querySelector(".point-popover-card"), null);

  dom.window.close();
});

test("user original answer popovers open on click for point, redundancy, and typo spans", async () => {
  const dom = installDom(`
    <div class="report-content">
      <div class="user-original-stats">
        <span class="stat-pill efficiency">作答总字数 280字 ｜ 采分有效 190字（68%）</span>
        <span class="stat-pill waste">冗余废话 90字（32%）</span>
      </div>
      <div class="user-structure-alert">⚠️ 考场体例结构扣分诊断：未体现三县主体结构</div>
      <p>
        <span class="user-point-span status-hit"
              tabindex="0" role="button"
              data-user-status="hit"
              data-user-status-name="完全命中"
              data-user-score="+2.5分 / 满分2.5分"
              data-user-label="建立契约化服务">健全涉企契约服务</span>
        <span class="user-redundant-span"
              tabindex="0" role="button"
              data-redundant-wasted="28字"
              data-redundant-reason="自创泛化套话，脱离采分要义">在全县范围内大力倡导责任意识与大局观</span>
        <span class="user-typo-span"
              tabindex="0" role="button"
              data-typo-reason="疑为“召开”">招考</span>
      </p>
    </div>
  `);
  const { initializeMasterAnswerPopovers } = await import(`../../static/js/grading.js?user-popover=${Date.now()}`);
  initializeMasterAnswerPopovers(new AbortController().signal);

  const pointSpan = document.querySelector(".user-point-span");
  const redundantSpan = document.querySelector(".user-redundant-span");
  const typoSpan = document.querySelector(".user-typo-span");

  // 1. Point span
  pointSpan.click();
  let popover = document.querySelector(".point-popover-card");
  assert.ok(popover, "Popover should open for user point span");
  assert.ok(popover.textContent.includes("完全命中"));
  assert.ok(popover.textContent.includes("建立契约化服务"));
  assert.ok(popover.textContent.includes("+2.5分 / 满分2.5分"));

  // 2. Redundant span
  redundantSpan.click();
  popover = document.querySelector(".point-popover-card");
  assert.ok(popover, "Popover should switch to redundancy span");
  assert.ok(popover.textContent.includes("考场冗余废话诊断"));
  assert.ok(popover.textContent.includes("28字"));
  assert.ok(popover.textContent.includes("自创泛化套话"));

  // 3. Typo span
  typoSpan.click();
  popover = document.querySelector(".point-popover-card");
  assert.ok(popover, "Popover should switch to typo span");
  assert.ok(popover.textContent.includes("错词/病句诊断"));
  assert.ok(popover.textContent.includes("疑为“召开”"));

  dom.window.close();
});

test("bilateral synchronized hover and peer master popover data", async () => {
  const dom = installDom(`
    <div class="left-pane">
      <span class="master-point-span status-hit"
            data-point-key="point-tech"
            data-point-status="hit"
            data-point-status-name="完全得分"
            data-point-score="+1.5分"
            data-point-eval="你的作答准确命中了数字赋能机制。"
            data-point-source="材料2第1段：『推进数字化改革』">数字赋能机制</span>
    </div>
    <div class="right-pane">
      <span class="user-point-span status-hit"
            data-point-key="point-tech"
            data-user-status="hit"
            data-user-status-name="完全命中"
            data-user-score="+1.5分"
            data-user-label="数字化赋能">推进数字化</span>
    </div>
  `);
  const { initializeMasterAnswerPopovers } = await import(`../../static/js/grading.js?bilateral=${Date.now()}`);
  initializeMasterAnswerPopovers(new AbortController().signal);

  const masterSpan = document.querySelector(".master-point-span");
  const userSpan = document.querySelector(".user-point-span");

  // Hover on user span -> both get .is-peer-hovered
  userSpan.dispatchEvent(new Event("mouseenter"));
  assert.ok(userSpan.classList.contains("is-peer-hovered"));
  assert.ok(masterSpan.classList.contains("is-peer-hovered"));

  // Mouse leave -> removed
  userSpan.dispatchEvent(new Event("mouseleave"));
  assert.ok(!userSpan.classList.contains("is-peer-hovered"));
  assert.ok(!masterSpan.classList.contains("is-peer-hovered"));

  // Hover on master span -> both get .is-peer-hovered
  masterSpan.dispatchEvent(new Event("mouseenter"));
  assert.ok(userSpan.classList.contains("is-peer-hovered"));
  assert.ok(masterSpan.classList.contains("is-peer-hovered"));
  masterSpan.dispatchEvent(new Event("mouseleave"));

  // Click user span -> popover enriched with peer master evaluation and material source
  userSpan.click();
  const popover = document.querySelector(".point-popover-card");
  assert.ok(popover, "Popover opens for user span");
  assert.ok(popover.textContent.includes("你的作答准确命中了数字赋能机制。"));
  assert.ok(popover.textContent.includes("数字赋能机制"));
  assert.ok(popover.textContent.includes("推进数字化改革"));

  dom.window.close();
});

