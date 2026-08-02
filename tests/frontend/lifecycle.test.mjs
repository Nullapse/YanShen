import assert from "node:assert/strict";
import test from "node:test";

import { JSDOM } from "jsdom";

const installDom = (url = "http://localhost/home") => {
  const dom = new JSDOM(
    `<!doctype html><html><body data-active-section="home"><main class="main"></main></body></html>`,
    { url },
  );
  const globals = [
    "AbortController",
    "CustomEvent",
    "Element",
    "Event",
    "FormData",
    "HTMLAnchorElement",
    "HTMLFormElement",
    "HTMLElement",
    "Node",
    "NodeFilter",
  ];
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.localStorage = dom.window.localStorage;
  globalThis.sessionStorage = dom.window.sessionStorage;
  Object.defineProperty(globalThis, "navigator", {
    value: dom.window.navigator,
    configurable: true,
  });
  globals.forEach((name) => {
    globalThis[name] = dom.window[name];
  });
  return dom;
};

test("filter state is namespaced and restored before page interaction", async () => {
  const dom = installDom("http://localhost/papers?region=%E6%B5%99%E6%B1%9F");
  document.body.dataset.activeSection = "papers";
  document.querySelector("main").innerHTML = `
    <form class="auto-filter">
      <select name="region"><option value="浙江" selected>浙江</option></select>
      <input name="per_page" value="99">
      <input name="q" value="">
    </form>
  `;
  const core = await import(`../../static/js/core.js?state=${Date.now()}`);
  core.viewState.section = "papers";
  const form = document.querySelector("form");

  assert.equal(core.restoreFilterState(form), false);
  assert.equal(
    localStorage.getItem("gongkao.viewState.v2:filters:papers:/papers"),
    "region=%E6%B5%99%E6%B1%9F",
  );

  dom.window.history.replaceState({}, "", "/papers");
  let restoredUrl = null;
  core.configureStateNavigation((url) => {
    restoredUrl = url;
  });
  assert.equal(core.restoreFilterState(form), true);
  assert.equal(restoredUrl.pathname, "/papers");
  assert.equal(restoredUrl.searchParams.get("region"), "浙江");
  dom.window.close();
});

test("mounting a partial page aborts the previous page lifecycle", async () => {
  const dom = installDom();
  const { mountPage } = await import(`../../static/app.js?mount=${Date.now()}`);

  mountPage();
  const firstController = window.__gongkaoPageAbortController;
  assert.equal(firstController.signal.aborted, false);

  mountPage();
  assert.equal(firstController.signal.aborted, true);
  assert.equal(window.__gongkaoPageAbortController.signal.aborted, false);
  dom.window.close();
});

test("multiline annotations do not paint blank line fragments", async () => {
  const dom = installDom("http://localhost/attempts/7");
  const annotations = await import(`../../static/js/annotations.js?render=${Date.now()}`);
  const editor = document.createElement("div");
  editor.setAttribute("contenteditable", "true");
  editor.setAttribute("data-text-annotation", "");
  editor.dataset.annotationType = "answer";
  editor.dataset.annotationId = "7";
  editor.dataset.highlightScope = "attempt-7";
  editor.dataset.savedAnnotations = JSON.stringify([{
    start: 0,
    end: 5,
    color: "yellow",
    note: "跨行笔记",
  }]);
  editor.textContent = "甲\n\n乙文";
  document.body.append(editor);

  annotations.renderTextAnnotations(editor);

  const marks = Array.from(editor.querySelectorAll(".text-annotation-highlight"));
  assert.deepEqual(marks.map((mark) => mark.textContent), ["甲", "乙文"]);
  assert.equal(marks.some((mark) => !mark.textContent.trim()), false);
  assert.equal(editor.querySelectorAll(".highlight-note-indicator").length, 1);
  assert.equal(annotations.editableValue(editor), "甲\n\n乙文");
  dom.window.close();
});

test("material annotation decorations never become source text", async () => {
  const dom = installDom("http://localhost/attempts/7");
  const annotations = await import(`../../static/js/annotations.js?material=${Date.now()}`);
  const material = document.createElement("div");
  material.setAttribute("data-material-highlight", "");
  material.dataset.materialId = "3";
  material.dataset.highlightScope = "attempt-7";
  material.dataset.savedAnnotations = JSON.stringify([{
    start: 2,
    end: 6,
    color: "yellow",
    note: "重点",
  }]);
  material.textContent = "材料正文没有额外换行。";
  document.body.append(material);

  annotations.renderTextAnnotations(material);
  annotations.renderTextAnnotations(material);

  assert.equal(annotations.editableValue(material), "材料正文没有额外换行。");
  assert.equal(material.querySelectorAll(".highlight-note-indicator").length, 1);
  dom.window.close();
});

test("annotation note popover edits from its content and deletes from the corner", async () => {
  const dom = installDom("http://localhost/attempts/7");
  const annotations = await import(`../../static/js/annotations.js?popover=${Date.now()}`);
  const material = document.createElement("div");
  material.setAttribute("data-material-highlight", "");
  material.dataset.materialId = "3";
  material.dataset.highlightScope = "attempt-7";
  material.dataset.savedAnnotations = JSON.stringify([{
    start: 0,
    end: 2,
    color: "yellow",
    note: "重点",
  }]);
  material.textContent = "材料正文";
  document.body.append(material);
  annotations.initializeAnnotations(new AbortController().signal);

  material.querySelector(".has-note").dispatchEvent(new dom.window.MouseEvent("click", {
    bubbles: true,
  }));
  const popover = document.querySelector(".annotation-popover-card");
  assert.equal(popover.classList.contains("active"), true);
  assert.equal(popover.querySelector(".annotation-popover-actions"), null);
  assert.equal(popover.querySelector(".annotation-popover-delete").textContent.trim(), "×");

  popover.querySelector(".annotation-popover-content").click();
  assert.equal(document.querySelector(".annotation-modal-overlay").classList.contains("active"), true);
  dom.window.close();
});

test("deep thinking is enabled by default and persists across page mounts", async () => {
  const dom = installDom("http://localhost/attempts/7");
  const grading = await import(`../../static/js/grading.js?preferences=${Date.now()}`);
  const firstForm = document.createElement("form");
  firstForm.innerHTML = `
    <input type="checkbox" name="use_deep_thinking" data-deep-thinking-preference checked>
  `;
  document.body.append(firstForm);
  const firstController = new AbortController();
  grading.initializeDeepThinkingPreference(firstForm, firstController.signal);
  const firstCheckbox = firstForm.querySelector("[data-deep-thinking-preference]");

  assert.equal(firstCheckbox.checked, true);
  firstCheckbox.checked = false;
  firstCheckbox.dispatchEvent(new Event("change"));
  firstController.abort();
  firstForm.remove();

  const secondForm = document.createElement("form");
  secondForm.innerHTML = `
    <input type="checkbox" name="use_deep_thinking" data-deep-thinking-preference checked>
  `;
  document.body.append(secondForm);
  grading.initializeDeepThinkingPreference(secondForm, new AbortController().signal);
  assert.equal(secondForm.querySelector("[data-deep-thinking-preference]").checked, false);
  dom.window.close();
});
