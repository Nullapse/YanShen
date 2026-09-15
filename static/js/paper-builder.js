function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value == null ? "" : String(value);
  return node.innerHTML;
}

function setStatus(element, message, error = false) {
  if (!element) return;
  element.innerHTML = `<span style="color:${error ? "#dc2626" : "#2563eb"};">${escapeHtml(message)}</span>`;
}

function initializeUrlImport({ addMaterialField, addQuestionField }) {
  const seedNode = document.getElementById("url-import-seed");
  const input = document.getElementById("url-import-input");
  const button = document.getElementById("url-import-button");
  const status = document.getElementById("url-import-status");
  const preview = document.getElementById("url-import-preview");
  const credentialPanel = document.getElementById("url-import-credentials");
  const cookieInput = document.getElementById("url-import-cookie");
  const deviceInput = document.getElementById("url-import-device-id");
  const rememberInput = document.getElementById("url-import-remember");
  const clearCredentialsButton = document.getElementById("url-import-clear-credentials");
  const credentialState = document.getElementById("url-import-credential-state");
  if (!input || !button || !status || !preview || button.dataset.urlImportInitialized === "1") return;

  button.dataset.urlImportInitialized = "1";
  window.urlImportDraft = null;
  window.urlImportSource = { url: input.value.trim(), kind: "" };
  window.urlImportBookmarklet = "";

  function updateCredentialState(result) {
    const credentials = result?.credentials;
    if (!credentialState || !credentials) return;
    if (!credentials.configured) {
      credentialState.textContent = "尚未保存";
      return;
    }
    credentialState.textContent = `已配置（${credentials.cookie_count || 0} 个 Cookie${credentials.device_id_present ? "，含 DeviceSid" : ""}）`;
  }

  function showDraft(draft) {
    window.urlImportDraft = draft || null;
    if (draft && draft.source_url) {
      window.urlImportSource = { url: draft.source_url, kind: draft.source_kind || "" };
      input.value = draft.source_url;
    }
    const materials = (draft && draft.materials) || [];
    const questions = (draft && draft.questions) || [];
    const references = questions.filter((item) => (
      item.reference_answer && item.reference_answer.answer_text
    )).length;
    preview.hidden = false;
    preview.innerHTML = `<strong style="color:#1e3a8a;">已解析预览</strong>
      <p style="margin:0.45rem 0;">${escapeHtml((draft && draft.paper_name) || "未命名套卷")}
      · ${questions.length} 道题 · ${materials.length} 则资料 · ${references} 份候选参考答案</p>
      <p class="muted" style="margin:0 0 0.75rem; font-size:0.82rem;">请先检查题干、材料和参考答案，再点击应用；保存后才会写入题库。</p>
      <button type="button" class="button secondary" id="url-import-apply">应用到下方表单</button>`;
    document.getElementById("url-import-apply")?.addEventListener("click", applyDraft);
  }

  function showCredentialPrompt(result) {
    if (credentialPanel) credentialPanel.open = true;
    preview.hidden = false;
    preview.innerHTML = `<strong style="color:#92400e;">需要粉笔登录态</strong>
      <p style="margin:0.45rem 0;">应用已经访问到粉笔接口，但粉笔没有返回完整套卷。请展开上面的“粉笔登录态”，把 Cookie-Editor 导出的 JSON 数组粘贴一次，然后再次点击“直接导入并预览”。</p>
      <p class="muted" style="margin:0; font-size:0.82rem;">成功后登录态会按你的选择保存在本机，后续批量导入只需要 URL。应用不会把 Cookie 写入题库或日志。</p>`;
    setStatus(status, result?.message || "请补充粉笔 Cookie 后重试。", true);
  }

  function showBridge(result) {
    window.urlImportBookmarklet = result.bookmarklet || "";
    preview.hidden = false;
    preview.innerHTML = `<strong style="color:#92400e;">需要已登录粉笔页面配合一次</strong>
      <p style="margin:0.45rem 0;">粉笔接口要求浏览器登录态。复制下面的书签脚本，新建一个浏览器书签（名称可填“导入到研申”），然后在已登录的粉笔解析页点击它。完成后会自动回到本页预览。</p>
      <div style="display:flex; gap:0.5rem; flex-wrap:wrap; margin-bottom:0.55rem;">
        <button type="button" class="button secondary" id="url-import-copy">复制书签脚本</button>
        <a class="button ghost" id="url-import-bookmark" href="#">拖动到浏览器书签栏（勿直接点击）</a>
      </div>
      <textarea id="url-import-bookmarklet" readonly rows="3" style="width:100%; box-sizing:border-box; font-size:0.78rem; padding:0.55rem; border:1px solid #fed7aa; border-radius:5px;"></textarea>
      <p class="muted" style="margin:0.55rem 0 0; font-size:0.8rem;">只读取当前粉笔页在浏览器中已经可以看到的数据；本地应用不接收 Cookie、密码或登录令牌。</p>`;
    const textarea = document.getElementById("url-import-bookmarklet");
    textarea.value = window.urlImportBookmarklet;
    const bookmark = document.getElementById("url-import-bookmark");
    bookmark.href = window.urlImportBookmarklet || "#";
    bookmark.addEventListener("click", (event) => {
      event.preventDefault();
      setStatus(status, "请先把此链接拖到浏览器书签栏，再在已登录的粉笔解析页点击书签；不要直接在研申页面点击。");
    });
    document.getElementById("url-import-copy")?.addEventListener("click", () => {
      const copied = () => setStatus(status, "书签脚本已复制，请在已登录粉笔页点击它。");
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(window.urlImportBookmarklet)
          .then(copied)
          .catch(() => {
            textarea.select();
            document.execCommand("copy");
            copied();
          });
      } else {
        textarea.select();
        document.execCommand("copy");
        copied();
      }
    });
    setStatus(status, result.message || "请按提示完成浏览器桥接。");
  }

  function renderResult(result) {
    updateCredentialState(result);
    if (result?.requires_credentials) {
      showCredentialPrompt(result);
      return;
    }
    if (result?.requires_browser_bridge) {
      showBridge(result);
      return;
    }
    if (!result || !result.ok || !result.draft) {
      setStatus(status, result?.error || result?.message || "没有得到可导入的内容。", true);
      return;
    }
    showDraft(result.draft);
    setStatus(status, result.stored_credentials ? "解析成功，粉笔登录态已保存。请检查后应用到表单。" : "解析成功。请检查后应用到表单。");
  }

  function applyDraft() {
    const draft = window.urlImportDraft || {};
    const name = document.getElementById("field-paper-name");
    if (name && draft.paper_name) name.value = draft.paper_name;
    const materialsContainer = document.getElementById("materials-container");
    const questionsContainer = document.getElementById("questions-container");
    materialsContainer.innerHTML = "";
    questionsContainer.innerHTML = "";
    (draft.materials || []).forEach((item) => addMaterialField(item.content || item.material_text || ""));
    (draft.questions || []).forEach((item) => {
      const reference = item.reference_answer || {};
      addQuestionField(item.prompt || item.original_text || "", reference.answer_text || "", reference);
    });
    if (!(draft.materials || []).length) addMaterialField();
    if (!(draft.questions || []).length) addQuestionField();
    setStatus(status, "已应用到表单；请检查内容后点击“保存卷子并开始写题”。");
    window.scrollTo({ top: (name?.offsetTop || 0) - 24, behavior: "smooth" });
  }

  async function importUrl() {
    const sourceUrl = input.value.trim();
    if (!sourceUrl) {
      setStatus(status, "请先粘贴粉笔套卷 URL。", true);
      input.focus();
      return;
    }
    button.disabled = true;
    preview.hidden = true;
    setStatus(status, "正在用本机粉笔登录态抓取完整材料和题目，请稍候……");
    try {
      const cookieText = cookieInput?.value?.trim() || "";
      const deviceId = deviceInput?.value?.trim() || "";
      const response = await fetch("/papers/import-url", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          source_url: sourceUrl,
          cookie_text: cookieText,
          device_id: deviceId,
          remember_session: rememberInput ? rememberInput.checked : true,
        }),
      });
      const result = await response.json();
      renderResult(result);
    } catch (error) {
      setStatus(status, `解析失败：${error.message || error}`, true);
    } finally {
      button.disabled = false;
    }
  }

  button.addEventListener("click", importUrl);

  clearCredentialsButton?.addEventListener("click", async () => {
    clearCredentialsButton.disabled = true;
    try {
      const response = await fetch("/papers/import-url", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ clear_credentials: true }),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "清除失败");
      if (cookieInput) cookieInput.value = "";
      if (deviceInput) deviceInput.value = "";
      if (credentialState) credentialState.textContent = "尚未保存";
      setStatus(status, "已清除本机保存的粉笔登录态。", false);
    } catch (error) {
      setStatus(status, `清除失败：${error.message || error}`, true);
    } finally {
      clearCredentialsButton.disabled = false;
    }
  });

  try {
    const seed = seedNode?.textContent?.trim();
    if (seed && seed !== "null") {
      renderResult({ ok: true, draft: JSON.parse(seed) });
      setStatus(status, "浏览器桥接已完成，请检查预览并应用。");
    }
  } catch (error) {
    setStatus(status, "导入预览数据读取失败，请重新解析 URL。", true);
  }
}

export function initializePaperBuilder() {
  const materialsContainer = document.getElementById("materials-container");
  const questionsContainer = document.getElementById("questions-container");
  if (!materialsContainer || !questionsContainer || materialsContainer.dataset.paperBuilderInitialized === "1") return;

  materialsContainer.dataset.paperBuilderInitialized = "1";

  function renumberMaterials() {
    const cards = document.querySelectorAll("#materials-container .material-card");
    cards.forEach((card, index) => {
      const badge = card.querySelector(".mat-badge");
      if (badge) badge.textContent = `给定资料 ${index + 1}`;
      const textarea = card.querySelector(".mat-content");
      if (textarea && !textarea.value) {
        textarea.placeholder = `请按顺序输入或粘贴第 ${index + 1} 篇给定资料正文...`;
      }
    });
  }

  function addMaterialField(initialText = "") {
    const container = document.getElementById("materials-container");
    if (!container) return;
    const div = document.createElement("div");
    div.className = "material-card";
    div.style.cssText = "border: 1px solid var(--line, #e2e8f0); border-radius: 6px; padding: 1rem; background: var(--bg-alt, #f8fafc);";
    div.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
        <strong class="mat-badge" style="color: var(--blue, #2563eb); font-size: 0.95rem;">给定资料</strong>
        <button type="button" class="button ghost small" onclick="removeMaterialField(this)" style="color: #ef4444; padding: 0.2rem 0.5rem;" title="删除此篇资料">✕ 删除</button>
      </div>
      <textarea class="mat-content" rows="6" placeholder="请按顺序输入或粘贴资料正文..." style="width: 100%; box-sizing: border-box; font-family: inherit; font-size: 0.92rem; padding: 0.75rem; border: 1px solid var(--line, #cbd5e1); border-radius: 6px;"></textarea>
    `;
    if (initialText) div.querySelector(".mat-content").value = initialText;
    container.appendChild(div);
    renumberMaterials();
  }

  function removeMaterialField(button) {
    const card = button.closest(".material-card");
    if (card) {
      card.remove();
      renumberMaterials();
    }
  }

  function renumberQuestions() {
    const cards = document.querySelectorAll("#questions-container .question-card-item");
    cards.forEach((card, index) => {
      const badge = card.querySelector(".q-badge");
      if (badge) badge.textContent = `第 ${index + 1} 题`;
    });
  }

  function addQuestionField(initialPrompt = "", initialRef = "", initialReferenceMeta = null) {
    const container = document.getElementById("questions-container");
    if (!container) return;
    const div = document.createElement("div");
    div.className = "question-card-item";
    div.style.cssText = "border: 1px solid var(--line, #e2e8f0); border-radius: 6px; padding: 1.25rem; background: var(--bg-alt, #f8fafc);";
    div.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
        <div style="display: flex; align-items: center; gap: 0.5rem;">
          <strong class="q-badge" style="color: var(--green, #16a34a); font-size: 1rem;">第 1 题</strong>
          <span style="font-size: 0.8rem; color: var(--muted, #64748b);">（题目 + 一份参考答案）</span>
        </div>
        <button type="button" class="button ghost small" onclick="removeQuestionField(this)" style="color: #ef4444; padding: 0.2rem 0.5rem;" title="删除此题">✕ 删除题目</button>
      </div>
      <div style="margin-bottom: 0.85rem;">
        <label style="display: block; font-weight: 600; font-size: 0.9rem; margin-bottom: 0.35rem; color: var(--text, #1e293b);">
          题目（题干与作答要求） <span style="color: #ef4444;">*</span>
        </label>
        <textarea class="q-prompt" rows="3" placeholder="在此输入题目内容与作答要求，例如：根据“给定资料1”，概括某某的主要做法。要求：全面、准确、有条理，不超过200字。（20分）" style="width: 100%; box-sizing: border-box; font-family: inherit; font-size: 0.92rem; padding: 0.75rem; border: 1px solid var(--line, #cbd5e1); border-radius: 6px;"></textarea>
      </div>
      <div>
        <label style="display: block; font-weight: 600; font-size: 0.9rem; margin-bottom: 0.35rem; color: var(--text, #1e293b);">
          参考答案（一份） <small class="muted" style="font-weight: normal;">（选填，AI 将以给定资料为最高真理源独立做题并进行纠错）</small>
        </label>
        <textarea class="q-ref-answer" rows="4" placeholder="在此输入此题的一份参考答案（选填。AI 将依据 Shenlun.skill 体系自主做题，参考答案不准确时绝不作为扣分依据）" style="width: 100%; box-sizing: border-box; font-family: inherit; font-size: 0.92rem; padding: 0.75rem; border: 1px solid var(--line, #cbd5e1); border-radius: 6px;"></textarea>
      </div>
    `;
    if (initialPrompt) div.querySelector(".q-prompt").value = initialPrompt;
    if (initialRef) div.querySelector(".q-ref-answer").value = initialRef;
    div.dataset.referenceMeta = JSON.stringify(initialReferenceMeta || {});
    container.appendChild(div);
    renumberQuestions();
  }

  function removeQuestionField(button) {
    const card = button.closest(".question-card-item");
    if (card) {
      card.remove();
      renumberQuestions();
    }
  }

  async function submitCustomPaper() {
    const nameInput = document.getElementById("field-paper-name");
    const paperName = (nameInput?.value || "").trim();
    const status = document.getElementById("submit-status");
    const button = document.getElementById("btn-save-paper");

    if (!paperName) {
      nameInput?.focus();
      if (status) status.innerHTML = '<span style="color: #ef4444; font-weight: 600;">请填写试卷名称或一句话描述</span>';
      return;
    }

    const materials = [];
    document.querySelectorAll("#materials-container .mat-content").forEach((element, index) => {
      const value = element.value.trim();
      if (value) materials.push({ material_number: index + 1, content: value, title: `给定资料${index + 1}` });
    });

    const questions = [];
    document.querySelectorAll("#questions-container .question-card-item").forEach((card, index) => {
      const prompt = (card.querySelector(".q-prompt")?.value || "").trim();
      const referenceText = (card.querySelector(".q-ref-answer")?.value || "").trim();
      if (!prompt) return;
      let referenceAnswer = referenceText;
      try {
        const meta = JSON.parse(card.dataset.referenceMeta || "{}");
        if (referenceText && Object.keys(meta).length) referenceAnswer = { ...meta, answer_text: referenceText };
      } catch (error) {
        referenceAnswer = referenceText;
      }
      questions.push({ question_number: index + 1, prompt, reference_answer: referenceAnswer });
    });

    if (!questions.length) {
      if (status) status.innerHTML = '<span style="color: #ef4444; font-weight: 600;">请至少录入一道题目的内容</span>';
      return;
    }

    if (status) status.innerHTML = '<span style="color: var(--blue, #2563eb);">正在保存试卷与题目...</span>';
    if (button) button.disabled = true;
    try {
      const response = await fetch("/papers/new", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          paper_name: paperName,
          materials,
          questions,
          source_url: window.urlImportSource?.url || "",
          source_kind: window.urlImportSource?.kind || "",
          source_note: window.urlImportDraft?.import_note || "",
        }),
      });
      const data = await response.json();
      if (data.ok && data.redirect) {
        if (status) status.innerHTML = '<span style="color: #16a34a; font-weight: 600;">✓ 保存成功，正在跳转...</span>';
        window.location.href = data.redirect;
      } else {
        if (status) status.innerHTML = `<span style="color: #ef4444; font-weight: 600;">保存失败: ${escapeHtml(data.error || "未知错误")}</span>`;
        if (button) button.disabled = false;
      }
    } catch (error) {
      if (status) status.innerHTML = `<span style="color: #ef4444; font-weight: 600;">网络异常: ${escapeHtml(error.message || error)}</span>`;
      if (button) button.disabled = false;
    }
  }

  // Existing markup uses inline handlers for the add/remove/save controls.
  // Expose the page-local functions so they keep working after partial navigation.
  window.addMaterialField = addMaterialField;
  window.removeMaterialField = removeMaterialField;
  window.addQuestionField = addQuestionField;
  window.removeQuestionField = removeQuestionField;
  window.submitCustomPaper = submitCustomPaper;

  if (!materialsContainer.querySelector(".material-card")) addMaterialField();
  if (!questionsContainer.querySelector(".question-card-item")) addQuestionField();
  initializeUrlImport({ addMaterialField, addQuestionField });
}
