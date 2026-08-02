import { editableValue } from "./annotations.js";
import { paragraphAlignmentsJson } from "./practice.js";

const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

function initializeGradingReviews(signal) {
  document.querySelectorAll("[data-grading-review]").forEach((review) => {
    const svg = review.querySelector(".grading-review-connectors");
    const documentPanel = review.querySelector(".grading-review-document");
    const sourceText = review.querySelector(".grading-annotation-source-text");
    if (!svg || !documentPanel || !sourceText) return;

    let frame = 0;
    const draw = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const reviewRect = review.getBoundingClientRect();
        const documentRect = documentPanel.getBoundingClientRect();
        const sourceRect = sourceText.getBoundingClientRect();
        const sourceLineHeight = parseFloat(getComputedStyle(sourceText).lineHeight) || 32;
        svg.replaceChildren();
        svg.setAttribute("viewBox", `0 0 ${reviewRect.width} ${reviewRect.height}`);

        review.querySelectorAll(".grading-annotation-note[data-annotation-id]").forEach((note) => {
          const id = note.dataset.annotationId;
          const marker = review.querySelector(
            `.grading-source-mark[data-annotation-id="${id}"], .grading-insert-anchor[data-annotation-id="${id}"]`,
          );
          if (!marker) return;
          const markerRect = marker.getClientRects()[0] || marker.getBoundingClientRect();
          const noteRect = note.getBoundingClientRect();
          const startX = markerRect.right - reviewRect.left + 3;
          const markerLine = Math.max(
            0,
            Math.round((markerRect.top - sourceRect.top) / sourceLineHeight),
          );
          const startY = sourceRect.top - reviewRect.top
            + (markerLine * sourceLineHeight)
            + (sourceLineHeight * 0.58);
          const edgeX = documentRect.right - reviewRect.left + 12;
          const endX = noteRect.left - reviewRect.left - 8;
          const endY = noteRect.top - reviewRect.top + 22;
          const controlX = edgeX + Math.max(8, (endX - edgeX) / 2);
          const path = document.createElementNS(SVG_NAMESPACE, "path");
          path.setAttribute(
            "d",
            `M ${startX} ${startY} H ${edgeX} C ${controlX} ${startY}, ${controlX} ${endY}, ${endX} ${endY}`,
          );
          const color = getComputedStyle(note).getPropertyValue("--annotation-color").trim();
          if (color) path.style.setProperty("--connector-color", color);
          svg.append(path);
        });
      });
    };

    const focusPair = (id) => {
      review.querySelectorAll(".is-review-focus").forEach((item) => item.classList.remove("is-review-focus"));
      review.querySelectorAll(`[data-annotation-id="${id}"]`).forEach((item) => {
        item.classList.add("is-review-focus");
      });
    };
    review.addEventListener("click", (event) => {
      const item = event.target.closest("[data-annotation-id]");
      if (item) focusPair(item.dataset.annotationId);
    }, { signal });

    const observer = "ResizeObserver" in window ? new ResizeObserver(draw) : null;
    observer?.observe(review);
    observer?.observe(documentPanel);
    signal.addEventListener("abort", () => {
      observer?.disconnect();
      window.cancelAnimationFrame(frame);
    }, { once: true });
    document.fonts?.ready.then(() => {
      if (!signal.aborted) draw();
    });
    draw();
  });
}

export const DEEP_THINKING_PREFERENCE_KEY = "gongkao.grading.deepThinking.v1";

export function initializeDeepThinkingPreference(form, signal) {
  const checkbox = form.querySelector("[data-deep-thinking-preference]");
  if (!checkbox) return;
  try {
    const explicit = checkbox.dataset.preferenceExplicit === "1";
    const stored = localStorage.getItem(DEEP_THINKING_PREFERENCE_KEY);
    if (!explicit && stored !== null) {
      checkbox.checked = stored === "1";
    } else {
      localStorage.setItem(DEEP_THINKING_PREFERENCE_KEY, checkbox.checked ? "1" : "0");
    }
    checkbox.addEventListener("change", () => {
      localStorage.setItem(DEEP_THINKING_PREFERENCE_KEY, checkbox.checked ? "1" : "0");
    }, { signal });
  } catch {
    // Storage can be unavailable in hardened WebView profiles; the server default still applies.
  }
}

export function initializeGrading(signal, navigatePartial) {
  initializeGradingReviews(signal);
  document.querySelectorAll("[data-grading-references]").forEach((form) => {
    initializeDeepThinkingPreference(form, signal);
    const checkboxes = Array.from(form.querySelectorAll("input[name='reference_id']"));
    const customAnswer = form.querySelector("textarea[name='custom_reference_answer']");
    const saveButton = form.querySelector("[data-reference-save]");
    const dirtyNote = form.querySelector("[data-reference-dirty-note]");
    const count = document.querySelector("[data-reference-count]");
    const copyButton = document.querySelector("[data-package-copy]");
    const downloadLink = document.querySelector("[data-package-download]");
    const gradeButton = document.querySelector("[data-grade-submit]");
    const apiPanel = gradeButton ? gradeButton.closest(".api-grade-panel") : null;

    const signature = () => JSON.stringify({
      ids: checkboxes.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value),
      custom: customAnswer ? customAnswer.value : "",
    });
    const initialSignature = signature();

    const updateState = () => {
      const selected = checkboxes.filter((checkbox) => checkbox.checked).length;
      const dirty = signature() !== initialSignature;
      if (count) count.textContent = `${selected} / ${checkboxes.length}`;
      if (saveButton) saveButton.hidden = !dirty;
      if (dirtyNote) dirtyNote.hidden = !dirty;
      if (copyButton) copyButton.disabled = dirty;
      if (downloadLink) {
        downloadLink.classList.toggle("is-disabled", dirty);
        downloadLink.setAttribute("aria-disabled", dirty ? "true" : "false");
      }
    };

    form.querySelector("[data-reference-select-all]")?.addEventListener("click", () => {
      checkboxes.forEach((checkbox) => { checkbox.checked = true; });
      updateState();
    });
    form.querySelector("[data-reference-clear]")?.addEventListener("click", () => {
      checkboxes.forEach((checkbox) => { checkbox.checked = false; });
      updateState();
    });
    checkboxes.forEach((checkbox) => checkbox.addEventListener("change", updateState));
    if (customAnswer) customAnswer.addEventListener("input", updateState);
    if (downloadLink) {
      downloadLink.addEventListener("click", (event) => {
        if (downloadLink.getAttribute("aria-disabled") === "true") {
          event.preventDefault();
        }
      });
    }
    if (copyButton) {
      copyButton.addEventListener("click", () => {
        const packageText = document.getElementById("grading-package");
        if (packageText && !copyButton.disabled) {
          navigator.clipboard.writeText(packageText.value);
        }
      });
    }
    form.addEventListener("submit", (event) => {
      if (event.submitter && event.submitter.hasAttribute("data-grade-submit")) {
        const answerEditor = document.querySelector("[data-answer-form] [data-answer-input]");
        if (answerEditor) {
          const snapshotFields = {
            answer_text: editableValue(answerEditor),
            answer_format_json: paragraphAlignmentsJson(answerEditor),
          };
          Object.entries(snapshotFields).forEach(([name, value]) => {
            let field = form.querySelector(`input[data-grading-answer-snapshot][name="${name}"]`);
            if (!field) {
              field = document.createElement("input");
              field.type = "hidden";
              field.name = name;
              field.dataset.gradingAnswerSnapshot = "";
              form.append(field);
            }
            field.value = value;
          });
        }
        event.submitter.disabled = true;
        event.submitter.textContent = "生成中...";
        if (apiPanel) apiPanel.classList.add("is-loading");
      }
    });
    updateState();
  });

  document.querySelectorAll("[data-grading-job]").forEach((panel) => {
    const jobId = panel.dataset.jobId;
    const message = panel.querySelector("[data-grading-job-message]");
    const progress = panel.querySelector("[data-grading-job-progress]");
    const bar = panel.querySelector("[data-grading-job-bar]");
    const error = panel.querySelector("[data-grading-job-error]");
    const gradeButton = document.querySelector("[data-grade-submit]");
    const pollingStartedAt = Date.now();
    let stopped = ["failed", "interrupted"].includes(panel.dataset.jobStatus || "");
    if (!stopped && gradeButton) gradeButton.disabled = true;

    const poll = async () => {
      if (signal.aborted || stopped || !jobId) return;
      try {
        const response = await fetch(`/grading-jobs/${jobId}/status`, { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "无法读取批改进度");
        if (message) {
          const waitedSeconds = Math.floor((Date.now() - pollingStartedAt) / 1000);
          const waitingSuffix = waitedSeconds >= 10
            && !["completed", "failed", "interrupted"].includes(payload.status)
            ? ` · 已等待 ${waitedSeconds} 秒`
            : "";
          message.textContent = `${payload.message || payload.status}${waitingSuffix}`;
        }
        if (progress) progress.textContent = `${payload.progress || 0}%`;
        if (bar) bar.value = payload.progress || 0;
        if (error) error.textContent = payload.error || "";
        panel.dataset.jobStatus = payload.status || "";
        panel.classList.toggle("is-error", ["failed", "interrupted"].includes(payload.status));
        if (payload.status === "completed") {
          stopped = true;
          const target = payload.report_id ? `#report-${payload.report_id}` : "";
          navigatePartial(
            new URL(`${window.location.pathname}${target}`, window.location.origin),
            { replace: true, silent: true },
          );
          return;
        }
        if (["failed", "interrupted"].includes(payload.status)) {
          stopped = true;
          if (payload.preview_available) {
            const target = new URL(window.location.href);
            target.searchParams.set("grading_job", String(jobId));
            target.hash = payload.preview_anchor || `grading-preview-${jobId}`;
            // The page already carries grading_job while polling. Changing only
            // the hash would not request freshly rendered HTML from the server.
            navigatePartial(target, { replace: true, silent: true });
            return;
          }
          if (gradeButton) gradeButton.disabled = false;
          return;
        }
      } catch (pollError) {
        if (error) error.textContent = pollError.message || "批改进度连接暂时中断，正在重试。";
      }
      window.setTimeout(poll, 900);
    };
    poll();
  });

  document.querySelectorAll("[data-preview-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const source = button.closest(".report-preview-card")?.querySelector("[data-preview-source]");
      if (!source) return;
      try {
        await navigator.clipboard.writeText(source.value || "");
        button.textContent = "已复制";
      } catch (error) {
        button.textContent = "复制失败";
      }
      window.setTimeout(() => { button.textContent = "复制报告"; }, 1600);
    });
  });
}
