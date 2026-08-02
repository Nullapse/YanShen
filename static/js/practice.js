import { anchorTextAnnotations, annotationSaveTimers, annotationStates, bindChineseAnswerPunctuation, bindPlainTextEditing, editableValue, readTextAnnotations, renderTextAnnotations, setEditableValue, textFingerprint } from "./annotations.js";
import { AutosaveCoordinator, answerGridCellsForLine, answerGridMetrics, answerLimit, answerLineCount, clampNumber, draftStorageKey, readDraft, readPaneWidth, removeDraft, showUndoToast, writeDraft, writePaneWidth } from "./core.js";
import { bindPaperSummaryTimer, bindPracticeTimer } from "./timers.js";

function paragraphLastLineCells(line) {
  const cells = answerGridCellsForLine(line);
  return cells ? ((cells - 1) % 25) + 1 : 0;
}

export function answerEditorDefaultHeight(wordLimit) {
  const values = String(wordLimit || "").match(/\d+/g)?.map(Number).filter(Number.isFinite) || [];
  const targetCharacters = values.length ? Math.max(...values) : 300;
  const rows = clampNumber(Math.ceil(targetCharacters / 25), 9, 24);
  return clampNumber(56 + rows * 25, 280, 656);
}

export function paragraphAlignments(editor) {
  const values = Array.from(editor?.__paragraphAlignments || []).map((value) => (
    ["center", "right"].includes(value) ? value : "left"
  ));
  while (values.at(-1) === "left") values.pop();
  return values;
}

export function paragraphAlignmentsJson(editor) {
  return JSON.stringify(paragraphAlignments(editor));
}

function caretLineCells(value, caretOffset) {
  const text = String(value || "").replace(/\r\n?/g, "\n");
  const offset = Math.max(0, Math.min(caretOffset || 0, text.length));
  // Caret right before a newline means the user placed the cursor at the end
  // of that paragraph line: report that line's own cell count.
  if (text[offset] === "\n") {
    const lineStart = text.lastIndexOf("\n", Math.max(0, offset - 1)) + 1;
    return paragraphLastLineCells(text.slice(lineStart, offset));
  }
  const lineStart = text.lastIndexOf("\n", Math.max(0, offset - 1)) + 1;
  const nextBreak = text.indexOf("\n", offset);
  const lineEnd = nextBreak === -1 ? text.length : nextBreak;
  if (offset >= lineEnd) {
    return paragraphLastLineCells(text.slice(lineStart));
  }
  return paragraphLastLineCells(text.slice(lineStart, offset));
}

function contenteditableCaretOffset(element) {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return editableValue(element).length;
  const range = selection.getRangeAt(0);
  const node = range.endContainer;
  const nodeOffset = range.endOffset;
  const segments = plainTextSegments(element);
  let acc = 0;
  for (const segment of segments) {
    if (segment.node === node) {
      if (node.nodeType === Node.TEXT_NODE) {
        return acc + Math.min(nodeOffset, segment.text.length);
      }
      return acc;
    }
    acc += segment.text.length;
  }
  return acc;
}

function plainTextSegments(root) {
  const blockTags = new Set(["DIV", "P", "LI", "SECTION", "ARTICLE", "HEADER", "FOOTER"]);
  const segments = [];
  const endsWithNewline = () => segments.length > 0 && segments[segments.length - 1].text.endsWith("\n");
  const pushText = (text, node) => {
    const clean = String(text || "").replace(/\r\n?/g, "\n");
    if (clean) segments.push({ text: clean, node });
  };
  const pushLineBreak = (node = null, synthetic = false) => {
    segments.push({ text: "\n", node, synthetic });
  };
  const pushSyntheticNewline = () => {
    if (segments.length && !endsWithNewline()) pushLineBreak(null, true);
  };
  const walk = (node) => {
    node.childNodes.forEach((child, index) => {
      if (child.nodeType === Node.TEXT_NODE) {
        pushText(child.nodeValue || "", child);
        return;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) return;
      if (child.matches?.("[data-editor-decoration]")) return;
      if (child.tagName === "BR") {
        pushLineBreak(child);
        return;
      }
      const isBlock = blockTags.has(child.tagName);
      if (isBlock && index > 0) pushSyntheticNewline();
      const lengthBefore = segments.length;
      walk(child);
      if (isBlock && segments.length === lengthBefore) pushSyntheticNewline();
    });
  };
  walk(root);
  while (segments.length && segments[segments.length - 1].synthetic) {
    segments.pop();
  }
  return segments;
}

export function initializePractice(signal) {
  document.querySelectorAll("[data-practice-timer]").forEach(bindPracticeTimer);
  document.querySelectorAll("[data-paper-summary-timer]").forEach(bindPaperSummaryTimer);
  initializeFavoriteToggles(signal);
  initializeEditorToolbars(signal);

  document.querySelectorAll("[data-answer-form]").forEach((form) => {
    const input = form.querySelector("[data-answer-input]");
    const hiddenInput = form.querySelector("[data-answer-hidden]");
    const answerFormatHidden = form.querySelector("[data-answer-format-hidden]");
    const annotationsHidden = form.querySelector("[data-annotations-hidden]");
    const annotationsHashHidden = form.querySelector("[data-annotations-hash-hidden]");
    const durationInput = form.querySelector("[data-duration-input]");
    const paperDurationInput = form.querySelector("[data-paper-duration-input]");
    const count = form.querySelector("[data-word-count]");
    const status = form.querySelector("[data-word-status]");
    const lineStatus = form.querySelector("[data-line-status]");
    const currentLineStatus = form.querySelector("[data-current-line-status]");
    const autosaveStatus = form.querySelector("[data-autosave-status]");
    const questionTimer = form.querySelector('[data-practice-timer][data-timer-kind="question"]')?.__practiceTimer;
    const limit = answerLimit(form.dataset.wordLimit);
    const dirtySubmit = form.hasAttribute("data-dirty-submit");
    const submitButton = dirtySubmit ? form.querySelector("button[type='submit']") : null;
    let serverSavedValue = editableValue(input);
    if (!input || !count || !status) return;
    if (input.classList.contains("answer-compose-editor")) {
      input.style.setProperty("--answer-editor-height", `${answerEditorDefaultHeight(form.dataset.wordLimit)}px`);
    }
    const storageKey = draftStorageKey(form);
    const savedDraft = readDraft(storageKey);
    const syncHiddenInput = () => {
      if (hiddenInput) hiddenInput.value = editableValue(input);
    };
    const syncAnswerFormat = () => {
      if (answerFormatHidden) answerFormatHidden.value = paragraphAlignmentsJson(input);
    };
    const syncAnnotationInputs = () => {
      if (!input?.matches("[data-text-annotation]")) return;
      const anchored = anchorTextAnnotations(readTextAnnotations(input), editableValue(input));
      annotationStates.set(input, anchored);
      if (annotationsHidden) annotationsHidden.value = JSON.stringify(anchored);
      if (annotationsHashHidden) annotationsHashHidden.value = textFingerprint(editableValue(input));
    };
    bindPlainTextEditing(input);
    bindChineseAnswerPunctuation(input);
    if (savedDraft !== null && savedDraft !== editableValue(input)) {
      setEditableValue(input, savedDraft);
      syncHiddenInput();
      if (autosaveStatus) autosaveStatus.textContent = "已恢复本地草稿";
    }

    let draftStatusTimer = 0;
    const updateAutosaveStatus = (message) => {
      if (autosaveStatus) autosaveStatus.textContent = message;
    };

    const saveDraft = () => {
      window.clearTimeout(draftStatusTimer);
      const ok = writeDraft(storageKey, editableValue(input));
      if (!ok) {
        updateAutosaveStatus("草稿保存失败");
        return;
      }
      draftStatusTimer = window.setTimeout(() => {
        updateAutosaveStatus(`草稿已自动保存 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
      }, 120);
    };

    const answerBody = (value) => {
      syncHiddenInput();
      syncAnswerFormat();
      syncAnnotationInputs();
      const body = new URLSearchParams();
      body.set("answer_text", value);
      if (answerFormatHidden) body.set("answer_format_json", answerFormatHidden.value || "[]");
      if (annotationsHidden) body.set("annotations_json", annotationsHidden.value || "[]");
      if (annotationsHashHidden) body.set("annotations_text_hash", annotationsHashHidden.value || "");
      return body;
    };

    const serverAutosave = form.dataset.autosaveUrl
      ? new AutosaveCoordinator({
          resourceKey: `answer:${input.dataset.attemptId || form.dataset.autosaveUrl}`,
          saveUrl: form.dataset.autosaveUrl,
          readValue: () => editableValue(input),
          readFingerprint: () => JSON.stringify({
            text: editableValue(input),
            alignments: paragraphAlignments(input),
          }),
          buildBody: answerBody,
          statusElement: autosaveStatus,
          onSaved: (value) => {
            serverSavedValue = value;
            removeDraft(storageKey);
            if (submitButton) submitButton.hidden = editableValue(input) === serverSavedValue;
          },
        })
      : null;

    const updateCounter = () => {
      const value = editableValue(input);
      syncHiddenInput();
      const gridMetrics = answerGridMetrics(value);
      const current = gridMetrics.occupiedCells;
      const currentLines = gridMetrics.lines;
      const limitLines = answerLineCount(limit ? limit - 1 : 0);
      count.textContent = String(current);
      if (lineStatus) {
        lineStatus.textContent = limit
          ? `行数：${currentLines}/${limitLines}`
          : `行数：${currentLines}`;
      }
      if (currentLineStatus) {
        const caretOffset = "value" in input
          ? (input.selectionEnd ?? value.length)
          : contenteditableCaretOffset(input);
        currentLineStatus.textContent = `本行：${caretLineCells(value, caretOffset)}/25格`;
      }
      form.classList.toggle("is-over-limit", Boolean(limit && current >= limit));
      if (!limit) {
        status.textContent = current ? "题目未标注字数上限" : "开始作答后实时统计";
      } else if (current >= limit) {
        status.textContent = `已超出硬限制，至少需减少 ${current - limit + 1} 字（必须低于 ${limit} 字）`;
      } else {
        status.textContent = `还可输入 ${limit - current - 1} 字（必须低于 ${limit} 字）`;
      }
      if (submitButton) {
        submitButton.hidden = value === serverSavedValue;
      }
    };

    input.addEventListener("input", () => {
      updateCounter();
      writeDraft(storageKey, editableValue(input));
      if (serverAutosave) {
        serverAutosave.markDirty();
      } else {
        saveDraft();
      }
    });
    input.addEventListener("gongkao:editor-format-change", () => {
      syncAnswerFormat();
      if (serverAutosave) serverAutosave.markDirty();
    }, signal ? { signal } : undefined);
    if ("value" in input) {
      input.addEventListener("select", updateCounter, signal ? { signal } : undefined);
      input.addEventListener("keyup", updateCounter, signal ? { signal } : undefined);
      input.addEventListener("click", updateCounter, signal ? { signal } : undefined);
    } else {
      document.addEventListener("selectionchange", () => {
        if (document.activeElement === input) updateCounter();
      }, signal ? { signal } : undefined);
    }
    form.addEventListener("submit", () => {
      syncHiddenInput();
      syncAnswerFormat();
      syncAnnotationInputs();
      window.clearTimeout(annotationSaveTimers.get(input));
      if (durationInput && questionTimer) {
        questionTimer.persist();
        durationInput.value = String(questionTimer.seconds());
      }
      if (paperDurationInput && questionTimer) {
        paperDurationInput.value = String(questionTimer.paperSeconds ? questionTimer.paperSeconds() : 0);
      }
      if (questionTimer && form.querySelector('[data-timer-clear-on-submit="1"]') && editableValue(input).trim()) {
        questionTimer.clear();
      }
      removeDraft(storageKey);
    });
    if (serverAutosave) {
      window.addEventListener("pagehide", () => serverAutosave.flushOnPageHide(), { signal: signal });
      window.addEventListener("gongkao:before-navigation", () => serverAutosave.flushOnPageHide(), { signal: signal });
    }
    updateCounter();
  });

  document.querySelectorAll("[data-attempt-note-input]").forEach((input) => {
    const status = input.closest(".attempt-note-panel")?.querySelector("[data-attempt-note-status]");
    const saveUrl = input.dataset.saveUrl;
    if (!saveUrl) return;
    bindPlainTextEditing(input);
    bindChineseAnswerPunctuation(input);
    const bodyFor = (value) => {
      const body = new URLSearchParams();
      body.set("personal_note", value);
      if (input.matches("[data-text-annotation]")) {
        const anchored = anchorTextAnnotations(readTextAnnotations(input), value);
        annotationStates.set(input, anchored);
        body.set("annotations_json", JSON.stringify(anchored));
        body.set("annotations_text_hash", textFingerprint(value));
      }
      return body;
    };
    const coordinator = new AutosaveCoordinator({
      resourceKey: `note:${input.dataset.attemptId || saveUrl}`,
      saveUrl,
      readValue: () => editableValue(input),
      buildBody: bodyFor,
      statusElement: status,
    });
    input.addEventListener("input", () => coordinator.markDirty());
    window.addEventListener("pagehide", () => coordinator.flushOnPageHide(), { signal: signal });
    window.addEventListener("gongkao:before-navigation", () => coordinator.flushOnPageHide(), { signal: signal });
  });

  document.querySelectorAll("[data-resizable-attempt-pane]").forEach((layout) => {
    const resizer = layout.querySelector("[data-pane-resizer]");
    if (!resizer) return;

    const storageKey = layout.dataset.resizeStorageKey || "gongkao.attemptPaneWidth";
    const minWidth = Number(layout.dataset.minSideWidth) || 240;
    const minQuestionWidth = Number(layout.dataset.minMainWidth) || 280;
    const defaultWidth = Number(layout.dataset.defaultSideWidth) || minWidth;
    const defaultRatio = Number(layout.dataset.defaultSideRatio) || 0;

    const availableMaxWidth = () => {
      const rect = layout.getBoundingClientRect();
      const styles = getComputedStyle(layout);
      const columns = styles.gridTemplateColumns.split(" ").filter(Boolean).length;
      if (columns <= 1) return 0;
      return Math.max(minWidth, rect.width - minQuestionWidth - 24);
    };

    const applyWidth = (width, persist = false) => {
      const maxAvailable = availableMaxWidth();
      if (!maxAvailable) return;
      const nextWidth = Math.round(clampNumber(width, minWidth, maxAvailable));
      layout.style.setProperty("--attempt-pane-width", `${nextWidth}px`);
      if (persist) writePaneWidth(storageKey, nextWidth);
    };

    const responsiveDefaultWidth = () => {
      if (!defaultRatio) return defaultWidth;
      return Math.round(layout.getBoundingClientRect().width * defaultRatio);
    };
    const savedWidth = readPaneWidth(storageKey);
    let usesResponsiveDefault = Boolean(defaultRatio && !savedWidth);
    applyWidth(savedWidth || responsiveDefaultWidth());

    let activePointerId = null;
    let mouseResizing = false;

    const beginResize = (event) => {
      if (availableMaxWidth() <= 0) return;
      event.preventDefault();
      layout.classList.add("is-resizing");
    };

    const moveResize = (event) => {
      if (!layout.classList.contains("is-resizing")) return;
      const rect = layout.getBoundingClientRect();
      applyWidth(rect.right - event.clientX);
    };

    const finishResize = (event) => {
      if (!layout.classList.contains("is-resizing")) return;
      const rect = layout.getBoundingClientRect();
      applyWidth(rect.right - event.clientX, true);
      usesResponsiveDefault = false;
      layout.classList.remove("is-resizing");
    };

    resizer.addEventListener("pointerdown", (event) => {
      activePointerId = event.pointerId;
      beginResize(event);
      if (resizer.setPointerCapture) {
        resizer.setPointerCapture(event.pointerId);
      }
    });

    resizer.addEventListener("pointermove", (event) => {
      if (activePointerId !== event.pointerId) return;
      moveResize(event);
    });

    const finishPointerResize = (event) => {
      if (activePointerId !== event.pointerId) return;
      finishResize(event);
      if (resizer.hasPointerCapture && resizer.hasPointerCapture(event.pointerId)) {
        resizer.releasePointerCapture(event.pointerId);
      }
      activePointerId = null;
    };

    resizer.addEventListener("pointerup", finishPointerResize);
    resizer.addEventListener("pointercancel", finishPointerResize);

    resizer.addEventListener("mousedown", (event) => {
      mouseResizing = true;
      beginResize(event);
    });
    window.addEventListener("mousemove", (event) => {
      if (!mouseResizing) return;
      moveResize(event);
    }, { signal: signal });
    window.addEventListener("mouseup", (event) => {
      if (!mouseResizing) return;
      finishResize(event);
      mouseResizing = false;
    }, { signal: signal });
    window.addEventListener("resize", () => {
      if (usesResponsiveDefault) {
        applyWidth(responsiveDefaultWidth());
        return;
      }
      const current = Number(getComputedStyle(layout).getPropertyValue("--attempt-pane-width").replace("px", ""));
      if (current) applyWidth(current);
    }, { signal: signal });
  });
}

export function initializeEditorToolbars(signal) {
  document.querySelectorAll("[data-editor-toolbar]").forEach((toolbar) => {
    const target = toolbar.dataset.editorTarget
      ? document.querySelector(toolbar.dataset.editorTarget)
      : null;
    if (!target) return;

    target.__usesParagraphAlignment = true;
    if (!target.__paragraphAlignments) {
      try {
        const saved = JSON.parse(target.dataset.paragraphAlignments || "[]");
        target.__paragraphAlignments = Array.isArray(saved) ? saved : [];
      } catch (error) {
        target.__paragraphAlignments = [];
      }
    }
    renderTextAnnotations(target);

    const inEditor = (node) => node && (node === target || target.contains(node));
    const textLengthInFragment = (fragment) => {
      fragment.querySelectorAll?.("[data-editor-decoration]").forEach((node) => node.remove());
      return fragment.textContent.length;
    };
    const offsetAtPoint = (node, offset) => {
      const lines = Array.from(target.querySelectorAll(":scope > [data-editor-line]"));
      if (!lines.length) {
        const range = document.createRange();
        range.selectNodeContents(target);
        range.setEnd(node, offset);
        return textLengthInFragment(range.cloneContents());
      }
      if (node === target) {
        return lines.slice(0, Math.min(offset, lines.length)).reduce(
          (total, line) => total + editableValue(line).length + 1,
          0,
        ) - (offset >= lines.length ? 1 : 0);
      }
      const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
      const line = element?.closest?.("[data-editor-line]");
      const lineIndex = lines.indexOf(line);
      if (lineIndex < 0) return null;
      const prefix = lines.slice(0, lineIndex).reduce(
        (total, item) => total + editableValue(item).length + 1,
        0,
      );
      const localRange = document.createRange();
      localRange.selectNodeContents(line);
      localRange.setEnd(node, offset);
      return prefix + textLengthInFragment(localRange.cloneContents());
    };
    const pointAtOffset = (absoluteOffset) => {
      const text = editableValue(target);
      const safeOffset = Math.max(0, Math.min(Number(absoluteOffset) || 0, text.length));
      const before = text.slice(0, safeOffset).split("\n");
      const lineIndex = before.length - 1;
      let remaining = before[lineIndex].length;
      const lines = Array.from(target.querySelectorAll(":scope > [data-editor-line]"));
      const line = lines[Math.min(lineIndex, lines.length - 1)] || target;
      const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          return node.parentElement?.closest("[data-editor-decoration]")
            ? NodeFilter.FILTER_REJECT
            : NodeFilter.FILTER_ACCEPT;
        },
      });
      let textNode = walker.nextNode();
      while (textNode) {
        const length = textNode.nodeValue.length;
        if (remaining <= length) return { node: textNode, offset: remaining };
        remaining -= length;
        textNode = walker.nextNode();
      }
      return { node: line, offset: line.childNodes.length };
    };
    const selectionSnapshot = () => {
      const selection = window.getSelection();
      if (!selection || !selection.rangeCount) return null;
      const range = selection.getRangeAt(0);
      if (!inEditor(range.startContainer) || !inEditor(range.endContainer)) return null;
      const start = offsetAtPoint(range.startContainer, range.startOffset);
      const end = offsetAtPoint(range.endContainer, range.endOffset);
      if (start === null || end === null) return null;
      return { start, end, collapsed: range.collapsed };
    };
    const saveSelection = () => {
      const snapshot = selectionSnapshot();
      if (snapshot) target.__savedSelection = snapshot;
    };
    const restoreSelection = (snapshot = target.__savedSelection) => {
      if (!snapshot) return;
      const start = pointAtOffset(snapshot.start);
      const end = pointAtOffset(snapshot.end);
      const range = document.createRange();
      range.setStart(start.node, start.offset);
      range.setEnd(end.node, end.offset);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    };
    const lineSpan = (snapshot) => {
      const text = editableValue(target);
      const lineAt = (offset) => text.slice(0, Math.max(0, offset)).split("\n").length - 1;
      const start = lineAt(snapshot.start);
      const endProbe = snapshot.collapsed ? snapshot.end : Math.max(snapshot.start, snapshot.end - 1);
      return { start, end: lineAt(endProbe) };
    };
    const syncAlignButtons = () => {
      const snapshot = target.__savedSelection || selectionSnapshot();
      const span = snapshot ? lineSpan(snapshot) : { start: 0, end: 0 };
      const values = new Set();
      for (let index = span.start; index <= span.end; index += 1) {
        values.add(target.__paragraphAlignments[index] || "left");
      }
      const align = values.size === 1 ? values.values().next().value : "";
      toolbar.querySelectorAll("[data-editor-align]").forEach((button) => {
        const value = button.dataset.editorAlign;
        const active = align === value;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    };
    const applyAlign = (align) => {
      const snapshot = target.__savedSelection || selectionSnapshot();
      if (!snapshot) return;
      const span = lineSpan(snapshot);
      for (let index = span.start; index <= span.end; index += 1) {
        target.__paragraphAlignments[index] = align;
      }
      renderTextAnnotations(target);
      target.dispatchEvent(new CustomEvent("gongkao:editor-format-change", { bubbles: true }));
      target.focus();
      restoreSelection(snapshot);
      syncAlignButtons();
    };
    toolbar.querySelectorAll("[data-editor-align]").forEach((button) => {
      button.addEventListener("mousedown", (event) => {
        event.preventDefault();
        saveSelection();
      }, signal ? { signal } : undefined);
      button.addEventListener("click", () => {
        applyAlign(button.dataset.editorAlign);
      }, signal ? { signal } : undefined);
    });
    target.addEventListener("keyup", saveSelection, signal ? { signal } : undefined);
    target.addEventListener("click", saveSelection, signal ? { signal } : undefined);
    if (target.isContentEditable) {
      document.addEventListener("selectionchange", () => {
        if (selectionSnapshot()) {
          saveSelection();
          syncAlignButtons();
        }
      }, signal ? { signal } : undefined);
    }
    syncAlignButtons();
  });
}

function initializeFavoriteToggles(signal) {
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.classList.contains("favorite-form")) return;
    event.preventDefault();
    const button = form.querySelector("button.favorite-button");
    if (!button || form.dataset.favoritePending === "1") return;
    const wasFavorite = button.classList.contains("active");
    const action = form.action;
    const body = new URLSearchParams(new FormData(form));
    const applyState = (nowFavorite) => {
      const label = nowFavorite ? "取消收藏" : "收藏";
      button.classList.toggle("active", nowFavorite);
      button.title = label;
      button.setAttribute("aria-label", label);
    };
    form.dataset.favoritePending = "1";
    button.disabled = true;
    fetch(action, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      body,
      redirect: "manual",
      credentials: "same-origin",
    })
      .then((response) => {
        if (response.type === "opaqueredirect" || response.status === 303 || response.ok) {
          // The server answers with a 303 back to the current page; with
          // redirect:manual that response never triggers a follow-up fetch.
        } else {
          throw new Error(`favorite failed: ${response.status}`);
        }
        const nowFavorite = !wasFavorite;
        applyState(nowFavorite);
        if (window.location.pathname === "/favorites" && !nowFavorite) {
          const card = form.closest(".question-card");
          if (card) {
            card.classList.add("is-removing");
            window.setTimeout(() => {
              card.remove();
              updateFavoriteCounts(form.action, -1);
              showFavoritesEmptyState();
            }, 160);
          }
        } else {
          const kindLabel = /\/papers\//.test(action) ? "试卷" : "题目";
          showUndoToast(nowFavorite ? `已收藏${kindLabel}` : `已取消收藏${kindLabel}`, () => {
            const revertBody = new URLSearchParams(new FormData(form));
            fetch(action, {
              method: "POST",
              headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
              body: revertBody,
              redirect: "manual",
              credentials: "same-origin",
            }).then((response) => {
              if (response.type === "opaqueredirect" || response.status === 303 || response.ok) {
                applyState(wasFavorite);
              }
            }).catch(() => {});
          });
        }
      })
      .catch(() => {
        applyState(wasFavorite);
      })
      .finally(() => {
        form.dataset.favoritePending = "";
        button.disabled = false;
      });
  }, signal ? { signal, capture: true } : { capture: true });
}

function updateFavoriteCounts(action, delta) {
  const isPaper = /\/papers\//.test(action);
  const needle = isPaper ? "收藏试卷" : "收藏题目";
  document.querySelectorAll(".page-head .metrics > div").forEach((item) => {
    const label = item.querySelector("span")?.textContent || "";
    if (label.includes(needle)) {
      const strong = item.querySelector("strong");
      if (strong) strong.textContent = String(Math.max(0, (Number(strong.textContent) || 0) + delta));
    }
  });
  document.querySelectorAll(".view-tabs a").forEach((tab) => {
    if (!tab.textContent.includes(isPaper ? "试卷" : "题目")) return;
    const badge = tab.querySelector("span");
    if (badge) badge.textContent = String(Math.max(0, (Number(badge.textContent) || 0) + delta));
  });
}

function showFavoritesEmptyState() {
  const grid = document.querySelector(".question-grid");
  if (!grid || grid.children.length) return;
  grid.insertAdjacentHTML(
    "beforeend",
    '<div class="empty-state"><h2>还没有收藏的内容</h2><p>在题目或试卷卡片上点击星标后，会集中显示在这里。</p></div>',
  );
}
