export function initializeHome(signal) {
  const homeClock = document.querySelector("[data-home-clock]");
  if (homeClock) {
    const renderHomeClock = () => {
      homeClock.textContent = new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "long",
        day: "numeric",
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date());
    };
    renderHomeClock();
    window.__gongkaoPageIntervals.push(window.setInterval(renderHomeClock, 30000));
  }

  // Home Plan Widget
  const planCard = document.querySelector(".home-plan-card");
  const planModal = document.querySelector("[data-home-plan-modal]");
  const planForm = document.querySelector("[data-home-plan-form]");
  if (planCard && planModal && planForm) {
    const storageKey = "gongkao.home-plan.v3";
    const listContainer = planCard.querySelector(".home-plan-list");
    const container = planModal.querySelector("[data-home-plan-editor-container]") || planModal.querySelector(".home-plan-editor") || planForm;

    const defaults = [
      { title: "复盘最近一次批改", target: "/attempts" },
      { title: "完成 1 道归纳概括题", target: "/?question_type=归纳概括&work_status=unattempted" },
      { title: "提出对策专项训练", target: "/?question_type=提出对策&work_status=unattempted" },
      { title: "完成 1 道综合分析题", target: "/?question_type=综合分析&work_status=unattempted" },
      { title: "积累规范表述与金句", target: "/statistics" },
    ];

    const esc = (str) => String(str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

    const resolveTargetLabel = (title, target) => {
      const t = String(title || "").trim();
      const tg = String(target || "").trim();
      if (tg.includes("/attempts") || t.includes("复盘") || t.includes("批改")) return "跳转至 批改记录与复盘";
      if (tg.includes("/papers") || t.includes("套卷") || t.includes("模拟") || t.includes("考")) return "跳转至 真题与定时套卷";
      if (tg.includes("归纳概括") || t.includes("归纳概括")) return "跳转至 题库 · 归纳概括";
      if (tg.includes("综合分析") || t.includes("综合分析")) return "跳转至 题库 · 综合分析";
      if (tg.includes("提出对策") || t.includes("提出对策")) return "跳转至 题库 · 提出对策";
      if (tg.includes("贯彻执行") || t.includes("公文") || t.includes("贯彻")) return "跳转至 题库 · 贯彻执行";
      if (tg.includes("文章写作") || t.includes("大作文") || t.includes("文章")) return "跳转至 题库 · 大作文专项";
      if (tg.includes("/statistics") || t.includes("积累") || t.includes("金句") || t.includes("表述")) return "跳转至 训练复盘与统计";
      return "跳转至 题库检索";
    };

    const resolveTargetHref = (title, target) => {
      if (target && target !== "auto") return target;
      const t = String(title || "").trim();
      if (t.includes("复盘") || t.includes("批改")) return "/attempts";
      if (t.includes("套卷") || t.includes("模拟")) return "/papers";
      if (t.includes("归纳概括")) return "/?question_type=归纳概括&work_status=unattempted";
      if (t.includes("综合分析")) return "/?question_type=综合分析&work_status=unattempted";
      if (t.includes("提出对策")) return "/?question_type=提出对策&work_status=unattempted";
      if (t.includes("贯彻执行") || t.includes("公文")) return "/?question_type=贯彻执行&work_status=unattempted";
      if (t.includes("文章写作") || t.includes("大作文")) return "/?question_type=文章写作&work_status=unattempted";
      if (t.includes("积累") || t.includes("金句")) return "/statistics";
      return "/questions";
    };

    const readSavedPlan = () => {
      try {
        const val = JSON.parse(localStorage.getItem(storageKey) || "null");
        return Array.isArray(val) && val.length > 0 ? val : null;
      } catch (_e) {
        return null;
      }
    };

    let currentPlan = readSavedPlan() || defaults;

    const renderPlanList = () => {
      if (!listContainer) return;
      listContainer.innerHTML = "";
      currentPlan.forEach((item, index) => {
        const href = resolveTargetHref(item.title, item.target);
        const label = resolveTargetLabel(item.title, href);
        const a = document.createElement("a");
        a.className = "home-plan-item";
        a.href = href;
        a.innerHTML = `<i>${index + 1}</i><div class="home-plan-item-content"><strong>${esc(item.title)}</strong><small class="home-plan-item-sub">${esc(label)}</small></div><b>➜</b>`;
        listContainer.appendChild(a);
      });
    };

    const syncPlanFromDOM = () => {
      if (!container) return;
      const inputs = Array.from(container.querySelectorAll("[data-home-plan-title]"));
      if (inputs.length) {
        currentPlan = inputs.map((input) => ({
          title: input.value.trim() || "专项训练任务",
          target: "auto",
        }));
      }
    };

    const renderDynamicEditor = () => {
      if (!container) return;
      container.innerHTML = "";
      currentPlan.forEach((item, index) => {
        const row = document.createElement("div");
        row.className = "home-plan-editor-row";
        row.style.cssText = "display:flex; align-items:center; gap:8px; margin-bottom:8px;";
        row.innerHTML = `
          <span style="font-size:13px; font-weight:700; color:var(--green-deep); min-width:44px;">任务 ${index + 1}</span>
          <input type="text" maxlength="48" value="${esc(item.title)}" data-home-plan-title="${index}" style="flex:1; padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:13px;">
          <button type="button" class="home-task-del" data-delete-task="${index}" style="padding:4px 10px; border:1px solid var(--line); border-radius:6px; background:#fff; cursor:pointer;">×</button>
        `;
        container.appendChild(row);
      });

      container.querySelectorAll("[data-delete-task]").forEach((delBtn) => {
        delBtn.addEventListener("click", () => {
          syncPlanFromDOM();
          const idx = parseInt(delBtn.dataset.deleteTask);
          if (currentPlan.length > 1) {
            currentPlan.splice(idx, 1);
            renderDynamicEditor();
          }
        });
      });
    };

    const openPlan = () => {
      renderDynamicEditor();
      planModal.removeAttribute("hidden");
      planModal.classList.add("is-open");
      planModal.style.display = "grid";
      document.body.classList.add("has-modal-open");
    };

    const closePlan = () => {
      planModal.setAttribute("hidden", "true");
      planModal.classList.remove("is-open");
      planModal.style.display = "none";
      document.body.classList.remove("has-modal-open");
    };

    // Bind open buttons
    document.querySelectorAll("[data-home-plan-open]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        openPlan();
      });
    });

    // Bind close buttons & background click
    planModal.querySelectorAll("[data-home-plan-close]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        closePlan();
      });
    });
    planModal.addEventListener("click", (e) => {
      if (e.target === planModal) closePlan();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closePlan();
    }, { signal: signal });

    // Bind "➕ 添加自定义任务" button inside modal
    const addBtn = planModal.querySelector("[data-home-add-row]");
    if (addBtn) {
      addBtn.addEventListener("click", (e) => {
        e.preventDefault();
        syncPlanFromDOM();
        if (currentPlan.length < 10) {
          currentPlan.push({ title: "新专项训练任务", target: "auto" });
          renderDynamicEditor();
        }
      });
    }

    // Bind Chips (推荐任务库) buttons inside modal
    planModal.querySelectorAll(".home-task-chip").forEach((chip) => {
      chip.addEventListener("click", (e) => {
        e.preventDefault();
        syncPlanFromDOM();
        const taskTitle = chip.dataset.addPoolTask
          || chip.textContent.replace(/^(?:✦|📝|⏱️|🔍|💡|✒️|📚|\s)*/u, "").trim();
        if (currentPlan.length < 10) {
          currentPlan.push({ title: taskTitle, target: "auto" });
          renderDynamicEditor();
        }
      });
    });

    // Save plan handler
    const savePlan = () => {
      syncPlanFromDOM();
      localStorage.setItem(storageKey, JSON.stringify(currentPlan));
      renderPlanList();
      closePlan();
    };

    planForm.addEventListener("submit", (e) => {
      e.preventDefault();
      savePlan();
    });

    const submitBtn = planForm.querySelector("button[type='submit']");
    if (submitBtn) {
      submitBtn.addEventListener("click", (e) => {
        e.preventDefault();
        savePlan();
      });
    }

    // Reset plan handler
    const resetBtn = planForm.querySelector("[data-home-plan-reset]");
    if (resetBtn) {
      resetBtn.addEventListener("click", (e) => {
        e.preventDefault();
        localStorage.removeItem(storageKey);
        currentPlan = defaults;
        renderPlanList();
        renderDynamicEditor();
      });
    }

    renderPlanList();
  }

document.querySelector(".home-secondary-action")?.remove();

const legacyHomePrimary = document.querySelector(".home-primary-action");
if (legacyHomePrimary && !legacyHomePrimary.querySelector(":scope > strong")) {
  const legacyLabel = Array.from(legacyHomePrimary.childNodes)
    .find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim())
    ?.textContent.trim()
    || legacyHomePrimary.textContent.replace(/[→➜]/g, "").trim()
    || "继续练习";
  const legacyDetail = legacyLabel.includes("复盘")
    ? "查看批改报告与复盘"
    : legacyLabel.includes("继续")
      ? "返回未完成作答"
      : "进入答题页 · 草稿自动保存";
  const label = document.createElement("strong");
  const detail = document.createElement("small");
  const arrow = document.createElement("b");
  label.textContent = legacyLabel;
  detail.textContent = legacyDetail;
  arrow.textContent = "➜";
  legacyHomePrimary.replaceChildren(label, detail, arrow);
}

// Keep arrows visually consistent when an already-running local server still
// serves the previous thin-arrow homepage markup.
document.querySelectorAll(".home-card-heading > a, .home-week-card > a").forEach((node) => {
  node.textContent = node.textContent.replace(/\s*[→➜›]\s*$/g, "");
});
document.querySelectorAll("a, button").forEach((element) => {
  element.childNodes.forEach((node) => {
    if (node.nodeType === Node.TEXT_NODE && /→/.test(node.textContent)) {
      node.textContent = node.textContent.replace(/→/g, "➜");
    }
  });
});

// Top-level "换一题" in-place random question switcher
document.addEventListener("click", (e) => {
  const switchBtn = e.target.closest("[data-home-switch-question]");
  if (!switchBtn) return;
  e.preventDefault();

  const continueCard = switchBtn.closest(".home-continue-card") || document.querySelector(".home-continue-card");
  if (!continueCard) return;

  let pool = [];
  try {
    const raw = continueCard.dataset.unattemptedPool || "[]";
    const txt = document.createElement("textarea");
    txt.innerHTML = raw;
    pool = JSON.parse(txt.value);
  } catch (err) {
    console.error("Pool parse error:", err);
    pool = [];
  }

  if (!pool.length) return;
  window._heroPoolIdx = ((window._heroPoolIdx || 0) + 1) % pool.length;
  const q = pool[window._heroPoolIdx];

  const esc = (str) => String(str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  const kickerSpan = continueCard.querySelector(".home-continue-head span");
  const contextP = continueCard.querySelector(".home-continue-body > p");
  const titleH2 = continueCard.querySelector(".home-continue-body h2");
  const metaDiv = continueCard.querySelector(".home-continue-body > div");
  const primaryA = continueCard.querySelector(".home-primary-action");
  const progressB = continueCard.querySelector(".home-draft-progress b");
  const progressStrong = continueCard.querySelector(".home-draft-progress strong");
  const progressP = continueCard.querySelector(".home-draft-progress p");

  if (kickerSpan) kickerSpan.innerHTML = "<i></i>从这一题开始";
  if (contextP) contextP.textContent = `${q.year} ${q.region} · ${q.exam_type}`;
  if (titleH2) titleH2.textContent = q.title;
  if (metaDiv) {
    let metaHtml = `<span>${esc(q.question_type)}</span>`;
    if (q.word_limit) metaHtml += `<span>${esc(q.word_limit)}</span>`;
    if (q.question_number) metaHtml += `<span>第${esc(q.question_number)}题</span>`;
    metaDiv.innerHTML = metaHtml;
  }
  if (primaryA) {
    primaryA.href = `/questions/${q.question_id}`;
    primaryA.innerHTML = "<strong>开始作答</strong><small>进入答题页 · 草稿自动保存</small><b>➜</b>";
  }
  if (progressB) progressB.style.width = "0%";
  if (progressStrong) progressStrong.innerHTML = "0<small> 字</small>";
  if (progressP) progressP.textContent = "◷ 尚未开始 · 内容自动保存在本地";
}, { signal });
}
