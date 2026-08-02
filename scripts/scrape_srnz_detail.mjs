import fs from "node:fs/promises";

const EXPORT_DIR = "C:/Users/lwq/Documents/gongkao/exports/srnz";

async function scrapeVisibleAnswers(tab) {
  return await tab.playwright.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    const answers = [];
    const seen = new Set();
    const addAnswer = (organization, answer, notes = "") => {
      organization = clean(organization) || "囊中可见答案";
      answer = clean(answer);
      if (!answer) return;
      const key = `${organization}\n${answer}`;
      if (seen.has(key)) return;
      seen.add(key);
      answers.push({ organization, answer, notes });
    };

    for (const item of document.querySelectorAll(".user-add-answer-item")) {
      const organization = item.querySelector(".answer-source")?.innerText || "";
      const answer = item.querySelector(".answer-content.no-copy")?.innerText || "";
      const actions = item.querySelector(".answer-actions")?.innerText || "";
      addAnswer(organization, answer, actions ? `网友补充；互动数据：${clean(actions)}` : "网友补充");
    }

    if (!answers.length) {
      const sources = [...document.querySelectorAll(".answer-source")];
      const contents = [...document.querySelectorAll(".no-copy")];
      for (let index = 0; index < contents.length; index += 1) {
        addAnswer(sources[index]?.innerText || "囊中可见答案", contents[index]?.innerText || "", "囊中对比当前可见答案");
      }
    }
    return answers;
  }, undefined, { timeoutMs: 10000 });
}

async function clickSupplementalAnswerTab(tab) {
  const tabs = tab.playwright.locator(".answer-tabs .tab-button");
  const count = await tabs.count();
  for (let index = 0; index < count; index += 1) {
    const label = (await tabs.nth(index).innerText({ timeoutMs: 1000 })).trim();
    if (label.includes("网友补充")) {
      await tabs.nth(index).click({ force: true, timeoutMs: 2000 });
      await tab.playwright.waitForTimeout(80);
      return true;
    }
  }
  if (count > 1) {
    await tabs.nth(count - 1).click({ force: true, timeoutMs: 2000 });
    await tab.playwright.waitForTimeout(80);
    return true;
  }
  return false;
}

export async function startSrnzDetail(browser, id) {
  const tab = await browser.tabs.new();
  const url = `https://www.srnz.net/community/studyRoom/paper/detail/${id}`;
  await tab.goto(url);

  const materialTabs = tab.playwright.locator(".materials-tabs .tab-button");
  const questionTabs = tab.playwright.locator(".question-tabs .tab-button");
  await materialTabs.waitFor({ state: "visible", timeoutMs: 5000 });
  await questionTabs.waitFor({ state: "visible", timeoutMs: 5000 });

  const headings = tab.playwright.locator("h2");
  const headingCount = await headings.count();
  const title = headingCount
    ? (await headings.nth(0).innerText({ timeoutMs: 5000 })).trim()
    : "";

  return {
    id,
    tab,
    url,
    title,
    materialCount: await materialTabs.count(),
    questionCount: await questionTabs.count(),
    materials: [],
    questions: [],
  };
}

export async function scrapeSrnzMaterials(state, start = 0, end = state.materialCount) {
  const tabs = state.tab.playwright.locator(".materials-tabs .tab-button");
  for (let index = start; index < Math.min(end, state.materialCount); index += 1) {
    await tabs.nth(index).click({ force: true, timeoutMs: 2000 });
    await state.tab.playwright.waitForTimeout(10);
    const editors = state.tab.playwright.locator(".materials-content .ql-editor");
    const editorCount = await editors.count();
    state.materials[index] = {
      number: index + 1,
      text: editorCount
        ? (await editors.nth(0).innerText({ timeoutMs: 3000 })).trim()
        : "",
    };
  }
  return { id: state.id, materials: state.materials.length };
}

export async function scrapeSrnzQuestionsAndFinish(state) {
  const tabs = state.tab.playwright.locator(".question-tabs .tab-button");
  for (let index = 0; index < state.questionCount; index += 1) {
    await tabs.nth(index).click({ force: true, timeoutMs: 2000 });
    await state.tab.playwright.waitForTimeout(20);
    const prompts = state.tab.playwright.locator(".question-section > .ql-editor");
    const promptCount = await prompts.count();
    const sources = state.tab.playwright.locator(".answer-source");
    const sourceCount = await sources.count();
    const answers = state.tab.playwright.locator(".no-copy");
    const answerCount = await answers.count();
    const firstOrganization = sourceCount
      ? (await sources.nth(0).innerText({ timeoutMs: 3000 })).trim()
      : "";
    const firstAnswer = answerCount
      ? (await answers.nth(0).innerText({ timeoutMs: 3000 })).trim()
      : "";
    const answerList = [];
    if (firstAnswer) {
      answerList.push({
        organization: firstOrganization || "囊中可见答案",
        answer: firstAnswer,
        notes: "囊中对比当前可见答案",
      });
    }
    await clickSupplementalAnswerTab(state.tab);
    for (const item of await scrapeVisibleAnswers(state.tab)) {
      if (!answerList.some((answer) => answer.organization === item.organization && answer.answer === item.answer)) {
        answerList.push(item);
      }
    }
    state.questions.push({
      number: index + 1,
      raw: promptCount
        ? (await prompts.nth(0).innerText({ timeoutMs: 3000 })).trim()
        : "",
      answerOrganization: firstOrganization,
      answer: firstAnswer,
      answers: answerList,
    });
  }

  const data = {
    id: String(state.id),
    title: state.title,
    url: state.url,
    materials: state.materials,
    questions: state.questions,
    exportedAt: new Date().toISOString(),
  };
  await fs.mkdir(EXPORT_DIR, { recursive: true });
  await fs.writeFile(
    `${EXPORT_DIR}/${state.id}.json`,
    JSON.stringify(data, null, 2),
    "utf8",
  );
  await state.tab.close();

  return {
    id: state.id,
    title: state.title,
    materials: state.materials.length,
    questions: state.questions.length,
    answers: state.questions.reduce((total, question) => total + (question.answers?.length || (question.answer ? 1 : 0)), 0),
  };
}

export async function scrapeSrnzAnswersOnly(browser, id) {
  const path = `${EXPORT_DIR}/${id}.json`;
  const existing = JSON.parse(await fs.readFile(path, "utf8"));
  const tab = await browser.tabs.new();
  const url = `https://www.srnz.net/community/studyRoom/paper/detail/${id}`;
  try {
    await tab.goto(url);
    await tab.playwright.waitForTimeout(500);
    const questionTabs = tab.playwright.locator(".question-tabs .tab-button");
    await questionTabs.waitFor({ state: "visible", timeoutMs: 5000 });
    const questionCount = await questionTabs.count();
    for (let index = 0; index < questionCount; index += 1) {
      await questionTabs.nth(index).click({ force: true, timeoutMs: 2000 });
      await tab.playwright.waitForTimeout(80);
      const sources = tab.playwright.locator(".answer-source");
      const sourceCount = await sources.count();
      const contents = tab.playwright.locator(".no-copy");
      const contentCount = await contents.count();
      const firstOrganization = sourceCount ? (await sources.nth(0).innerText({ timeoutMs: 3000 })).trim() : "";
      const firstAnswer = contentCount ? (await contents.nth(0).innerText({ timeoutMs: 3000 })).trim() : "";
      const answerList = [];
      if (firstAnswer) {
        answerList.push({
          organization: firstOrganization || "囊中可见答案",
          answer: firstAnswer,
          notes: "囊中对比当前可见答案",
        });
      }
      await clickSupplementalAnswerTab(tab);
      for (const item of await scrapeVisibleAnswers(tab)) {
        if (!answerList.some((answer) => answer.organization === item.organization && answer.answer === item.answer)) {
          answerList.push(item);
        }
      }
      if (!existing.questions[index]) {
        existing.questions[index] = { number: index + 1, raw: "", answerOrganization: "", answer: "" };
      }
      existing.questions[index].answerOrganization = firstOrganization;
      existing.questions[index].answer = firstAnswer;
      existing.questions[index].answers = answerList;
    }
    existing.exportedAt = new Date().toISOString();
    existing.answerExportedAt = new Date().toISOString();
    await fs.writeFile(path, JSON.stringify(existing, null, 2), "utf8");
    return {
      id,
      title: existing.title,
      questions: existing.questions.length,
      answers: existing.questions.reduce((total, question) => total + (question.answers?.length || 0), 0),
    };
  } finally {
    await tab.close();
  }
}

export async function scrapeSrnzDetail(browser, id) {
  const tab = await browser.tabs.new();
  const url = `https://www.srnz.net/community/studyRoom/paper/detail/${id}`;

  try {
    await tab.goto(url);
    await tab.playwright.waitForTimeout(120);

    const headings = tab.playwright.locator("h2");
    let headingCount = await headings.count();
    let title = headingCount
      ? (await headings.nth(0).innerText({ timeoutMs: 5000 })).trim()
      : "";
    if (!title) {
      await tab.playwright.waitForTimeout(250);
      headingCount = await headings.count();
      title = headingCount
        ? (await headings.nth(0).innerText({ timeoutMs: 5000 })).trim()
        : "";
    }

    const materials = [];
    const materialTabs = tab.playwright.locator(".materials-tabs .tab-button");
    await materialTabs.waitFor({ state: "visible", timeoutMs: 5000 });
    const materialCount = await materialTabs.count();
    for (let index = 0; index < materialCount; index += 1) {
      await materialTabs.nth(index).click({ force: true, timeoutMs: 2000 });
      await tab.playwright.waitForTimeout(10);
      const editors = tab.playwright.locator(".materials-content .ql-editor");
      const editorCount = await editors.count();
      materials.push({
        number: index + 1,
        text: editorCount
          ? (await editors.nth(0).innerText({ timeoutMs: 5000 })).trim()
          : "",
      });
    }

    const questions = [];
    const questionTabs = tab.playwright.locator(".question-tabs .tab-button");
    await questionTabs.waitFor({ state: "visible", timeoutMs: 5000 });
    const questionCount = await questionTabs.count();
    for (let index = 0; index < questionCount; index += 1) {
      await questionTabs.nth(index).click({ force: true, timeoutMs: 2000 });
      await tab.playwright.waitForTimeout(20);

      const promptEditors = tab.playwright.locator(".question-section > .ql-editor");
      const promptCount = await promptEditors.count();
      const sources = tab.playwright.locator(".answer-source");
      const sourceCount = await sources.count();
      const answers = tab.playwright.locator(".no-copy");
      const answerCount = await answers.count();

      questions.push({
        number: index + 1,
        raw: promptCount
          ? (await promptEditors.nth(0).innerText({ timeoutMs: 5000 })).trim()
          : "",
        answerOrganization: sourceCount
          ? (await sources.nth(0).innerText({ timeoutMs: 5000 })).trim()
          : "",
        answer: answerCount
          ? (await answers.nth(0).innerText({ timeoutMs: 5000 })).trim()
          : "",
      });
    }

    const data = {
      id: String(id),
      title,
      url,
      materials,
      questions,
      exportedAt: new Date().toISOString(),
    };
    await fs.mkdir(EXPORT_DIR, { recursive: true });
    await fs.writeFile(
      `${EXPORT_DIR}/${id}.json`,
      JSON.stringify(data, null, 2),
      "utf8",
    );

    return {
      id,
      title,
      materials: materialCount,
      questions: questionCount,
      answers: questions.filter((question) => question.answer).length,
    };
  } finally {
    await tab.close();
  }
}
