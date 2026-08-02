import { escapeHtml, paragraphHtml } from "./core.js";

export function initializeAgent(signal, navigatePartial) {
  const phases = [
    ["classify_module", "理解问题"],
    ["load_user_context", "读取训练资料"],
    ["retrieve_candidates", "筛选上下文"],
    ["build_rag_context", "检索证据"],
    ["ChatOpenAI", "生成回复"],
  ];
  const renderStatusSteps = (steps) => {
    const names = new Set((steps || []).map((step) => step.tool_name));
    let currentUsed = false;
    return phases.map(([toolName, label]) => {
      let state = "todo";
      let prefix = "";
      if (names.has(toolName)) {
        state = "done";
        prefix = "✓ ";
      } else if (!currentUsed) {
        state = "current";
        currentUsed = true;
      }
      return `<span class="${state}">${escapeHtml(prefix + label)}</span>`;
    }).join("");
  };

  const agentStream = document.querySelector(".agent-message-stream");
  if (agentStream) {
    agentStream.scrollTop = agentStream.scrollHeight;
  }

  const unlockComposer = () => {
    document.querySelectorAll("[data-agent-composer]").forEach((form) => {
      delete form.dataset.submitting;
      const textarea = form.querySelector("textarea[name='message']");
      if (textarea) {
        textarea.readOnly = false;
        textarea.value = "";
      }
      const submitButton = form.querySelector("button[type='submit']");
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = "发送";
      }
      const footerNote = form.querySelector(".agent-composer-footer .muted");
      if (footerNote) {
        footerNote.textContent = "上下文会保留在当前线程中";
      }
    });
  };

  if (document.querySelector("[data-agent-pending]") || document.querySelector("[data-agent-awaiting]")) {
    const pollStatus = () => {
      if (signal.aborted || document.hidden) return;
      const statusUrl = `${window.location.pathname.replace(/\/$/, "")}/status`;
      fetch(statusUrl, { cache: "no-store" })
        .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`status failed: ${response.status}`))))
        .then((payload) => {
          if (payload.deleted) {
            navigatePartial(new URL("/agent", window.location.origin), { replace: true });
            return;
          }
          if (!payload.pending && !payload.awaiting) {
            unlockComposer();
            const pendingCard = document.querySelector("[data-agent-pending]");
            const stream = document.querySelector(".agent-message-stream");
            if (pendingCard && payload.message_html) {
              pendingCard.insertAdjacentHTML("afterend", payload.message_html);
              pendingCard.remove();
              if (stream) {
                stream.removeAttribute("data-agent-awaiting");
                stream.scrollTop = stream.scrollHeight;
              }
              return;
            }
            if (stream && payload.message_html && !document.querySelector(`[data-agent-message-id="${payload.latest_message_id}"]`)) {
              stream.insertAdjacentHTML("beforeend", payload.message_html);
              stream.removeAttribute("data-agent-awaiting");
              stream.scrollTop = stream.scrollHeight;
              return;
            }
            navigatePartial(new URL(window.location.href), { replace: true });
            return;
          }
          const pendingCard = document.querySelector("[data-agent-pending]");
          let box = pendingCard?.querySelector(".agent-status-steps");
          if (!box && pendingCard) {
            box = document.createElement("div");
            box.className = "agent-status-steps";
            box.setAttribute("aria-label", "真实生成进度");
            const note = pendingCard.querySelector(".agent-status-note");
            if (note) {
              note.insertAdjacentElement("afterend", box);
            } else {
              pendingCard.append(box);
            }
          }
          if (box && payload.steps) {
            box.innerHTML = renderStatusSteps(payload.steps);
          }
        })
        .catch((err) => {
          if (err?.message?.includes("404") || !window.location.pathname.startsWith("/agent/conversations/")) {
            return;
          }
          const note = document.querySelector("[data-agent-pending] .agent-status-note");
          if (note) {
            note.textContent = "后台仍在处理，暂时无法读取状态，稍后会继续尝试。";
          }
        });
    };
    window.setTimeout(pollStatus, 1200);
    window.__gongkaoPageIntervals.push(window.setInterval(pollStatus, 3000));
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) pollStatus();
    }, { signal: signal });
    window.addEventListener("focus", pollStatus, { signal: signal });
  }

  document.querySelectorAll("[data-agent-composer]").forEach((form) => {
    const textarea = form.querySelector("textarea[name='message']");
    const stream = form.closest(".agent-main")?.querySelector(".agent-message-stream");
    const submitButton = form.querySelector("button[type='submit']");
    const footerNote = form.querySelector(".agent-composer-footer .muted");
    form.addEventListener("submit", (event) => {
      if (!textarea) return;
      const message = textarea.value.trim();
      if (!message) {
        event.preventDefault();
        textarea.focus();
        return;
      }
      if (form.dataset.submitting === "1") {
        event.preventDefault();
        return;
      }
      form.dataset.submitting = "1";
      if (stream) {
        const now = new Date().toLocaleString("zh-CN", {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        });
        stream.insertAdjacentHTML("beforeend", `
          <article class="agent-message user">
            <header><strong>我</strong><span>${escapeHtml(now)}</span></header>
            <div class="report-body">${paragraphHtml(message)}</div>
          </article>
          <article class="agent-message assistant is-pending" data-agent-pending="1">
            <header><strong>AI 教练</strong><span>响应中</span></header>
            <div class="report-body">
              <p>正在读取训练上下文并生成回复。</p>
            </div>
            <div class="agent-status-note">后台任务已提交，正在读取真实执行步骤。</div>
            <div class="agent-status-steps" aria-label="真实生成进度">${renderStatusSteps([])}</div>
            <div class="agent-thinking-dots" aria-label="响应中"><span></span><span></span><span></span></div>
          </article>
        `);
        stream.scrollTop = stream.scrollHeight;
      }
      textarea.readOnly = true;
      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = "响应中...";
      }
      if (footerNote) {
        footerNote.textContent = "正在生成回复，请稍等";
      }
    });
  });
}
