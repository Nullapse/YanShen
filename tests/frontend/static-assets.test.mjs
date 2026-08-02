import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";

const readAsset = (name) => readFile(new URL(`../../static/${name}`, import.meta.url), "utf8");
const readDirectory = async (name) => {
  const directory = new URL(`../../static/${name}/`, import.meta.url);
  const filenames = (await readdir(directory)).filter((filename) => filename.endsWith(`.${name}`)).sort();
  return Promise.all(filenames.map((filename) => readFile(new URL(filename, directory), "utf8")));
};

test("browser assets are valid UTF-8 and retain stable state keys", async () => {
  const [entryScript, entryStyle, scripts, styles] = await Promise.all([
    readAsset("app.js"),
    readAsset("app.css"),
    readDirectory("js"),
    readDirectory("css"),
  ]);
  const script = [entryScript, ...scripts].join("\n");
  const stylesheet = [entryStyle, ...styles].join("\n");

  assert.equal(script.includes("\uFFFD"), false);
  assert.equal(stylesheet.includes("\uFFFD"), false);
  assert.match(script, /gongkao\.viewState\.v2/);
  assert.match(script, /gongkao\.answerDraft/);
  assert.match(script, /display:/);
  assert.match(script, /requestInit:\s*\{\s*method:\s*"POST"/);
  assert.match(script, /form\.requestSubmit\(event\.submitter/);
  assert.match(script, /playPartialViewTransition/);
  assert.match(script, /partialViewTransitionSteps/);
  assert.match(script, /setTimeout\(\(\) => \{\s*if \(url\.href === window\.location\.href\) return;/);
  assert.match(script, /window\.scrollTo\([\s\S]*playPartialViewTransition\(currentMain/);
  assert.doesNotMatch(script, /startFromActivity/);
  assert.match(script, /answer_format_json/);
  assert.match(script, /gradingAnswerSnapshot/);
  assert.match(script, /staged:\s*previousSection === "home" && nextSection === "papers"/);
  assert.match(stylesheet, /\.main\.is-view-entering \.is-view-step-entering/);
  assert.match(stylesheet, /@keyframes ui-view-step-arrive/);
  assert.doesNotMatch(script, /suppressPartialPageEntryAnimations/);
  assert.doesNotMatch(stylesheet, /animation:\s*workflow-arrive/);
  assert.match(stylesheet, /html\s*\{\s*scrollbar-gutter:\s*stable/);
  assert.match(stylesheet, /\.answer-compose-editor\s*\{[^}]*--answer-editor-height/s);
  assert.match(stylesheet, /\.answer-compose-editor\s*\{[^}]*height:\s*auto/s);
  assert.match(stylesheet, /\.answer-compose-editor\s*\{[^}]*overflow-y:\s*visible/s);
  assert.match(stylesheet, /\.sidebar-brand-row,[\s\S]*animation:\s*none\s*!important/);
  assert.match(stylesheet, /\.main\.is-partial-loading\s*\{[^}]*opacity:\s*1/s);
  assert.match(entryScript, /export function mountPage/);
  assert.match(entryScript, /playPartialViewTransition\(document\.querySelector\("\.main"\)\)/);
  assert.match(entryStyle, /@import url/);
});
