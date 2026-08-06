import { adaptivePageSize, rememberFilterState, showConfirmModal, submitForm, viewState } from "./core.js";
import { fetchLibraryListUpdate } from "./navigation.js";



export function initializeGlobalForms(signal) {
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.dataset.confirmed === "1") {
      return;
    }
    const confirmMessage = form.dataset.confirm;
    if (confirmMessage) {
      event.preventDefault();
      showConfirmModal(confirmMessage, () => {
        form.dataset.confirmed = "1";
        form.requestSubmit(event.submitter || undefined);
      });
      return;
    }
    const onsubmitAttr = form.getAttribute("onsubmit") || "";
    const match = onsubmitAttr.match(/confirm\(['"](.*?)['"]\)/);
    if (match) {
      event.preventDefault();
      const message = match[1];
      showConfirmModal(message, () => {
        form.dataset.confirmed = "1";
        form.requestSubmit(event.submitter || undefined);
      });
    }
  }, {
    signal,
    capture: true,
  });
}




export function initializeShellControls(signal) {
  const shell = document.querySelector(".shell");
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  if (shell && sidebarToggle) {
    const storageKey = "gongkao.sidebarMode";

    const storedMode = () => {
      try {
        return viewState.persistentGet(storageKey) === "top" ? "top" : "side";
      } catch (error) {
        return "side";
      }
    };

    const saveMode = (mode) => {
      try {
        viewState.persistentSet(storageKey, mode);
      } catch (error) {
        // Sidebar placement is a preference; failing to persist should not break the page.
      }
    };

    const isDesktopSidebar = () => window.matchMedia("(min-width: 1121px)").matches;

    const applySidebarMode = (mode, persist = false) => {
      const topMode = mode === "top";
      shell.classList.toggle("sidebar-top", topMode && isDesktopSidebar());
      const label = topMode ? "Switch navigation to left" : "Switch navigation to top";
      sidebarToggle.setAttribute("aria-label", label);
      sidebarToggle.setAttribute("title", label);
      if (persist) saveMode(topMode ? "top" : "side");
    };

    const syncSidebarMode = () => {
      if (isDesktopSidebar()) {
        applySidebarMode(storedMode());
      } else {
        shell.classList.remove("sidebar-top");
      }
    };

    sidebarToggle.addEventListener("click", () => {
      if (!isDesktopSidebar()) return;
      applySidebarMode(shell.classList.contains("sidebar-top") ? "side" : "top", true);
    });

    syncSidebarMode();
    window.addEventListener("resize", syncSidebarMode, { signal: signal });
  }

  document.querySelectorAll("[data-filter-panel-toggle]").forEach((toggle) => {
    const panelId = toggle.getAttribute("aria-controls");
    const panel = panelId ? document.getElementById(panelId) : null;
    if (!panel) return;
    const openStateKey = `filter-panel-open:${window.location.pathname}`;
    const syncFilterPanel = (expanded) => {
      panel.hidden = !expanded;
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggle.classList.toggle("is-expanded", expanded);
    };
    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") !== "true";
      syncFilterPanel(expanded);
      viewState.sessionSet(openStateKey, expanded ? "1" : "0");
    });
    const sessionOpen = viewState.sessionGet(openStateKey);
    if (sessionOpen === "1") {
      syncFilterPanel(true);
    } else {
      syncFilterPanel(!panel.hidden);
    }
  });

  // Actively opened filter panels stay open across same-route refreshes, but
  // collapse again when the user leaves the page (or restarts the app).
  window.addEventListener("gongkao:before-navigation", (event) => {
    const detail = event?.detail;
    const nextPathname = detail?.url ? new URL(detail.url, window.location.href).pathname : null;
    if (nextPathname && nextPathname !== window.location.pathname) {
      viewState.sessionRemove(`filter-panel-open:${window.location.pathname}`);
    }
  }, signal ? { signal } : undefined);

  document.querySelectorAll("[data-year-range]").forEach((range) => {
    const minYear = Number(range.dataset.minYear || 2020);
    const maxYear = Number(range.dataset.maxYear || new Date().getFullYear());
    const fromSlider = range.querySelector("[data-year-from]");
    const toSlider = range.querySelector("[data-year-to]");
    const fromHidden = range.querySelector("[data-year-from-hidden]");
    const toHidden = range.querySelector("[data-year-to-hidden]");
    const label = range.querySelector("[data-year-range-label]");
    if (!fromSlider || !toSlider || !fromHidden || !toHidden || !label) return;

    const clamp = (value) => Math.min(Math.max(Number(value) || minYear, minYear), maxYear);
    const syncYears = (changed) => {
      let from = clamp(fromSlider.value);
      let to = clamp(toSlider.value);
      if (from > to) {
        if (changed === "from") {
          to = from;
          toSlider.value = String(to);
        } else {
          from = to;
          fromSlider.value = String(from);
        }
      }
      fromHidden.value = String(from);
      toHidden.value = to >= maxYear ? "" : String(to);
      label.textContent = `${from} - ${to}`;
      const lower = ((from - minYear) / (maxYear - minYear || 1)) * 100;
      const upper = ((to - minYear) / (maxYear - minYear || 1)) * 100;
      range.style.setProperty("--range-left", `${lower}%`);
      range.style.setProperty("--range-right", `${100 - upper}%`);
    };

    fromSlider.addEventListener("input", () => syncYears("from"));
    toSlider.addEventListener("input", () => syncYears("to"));
    syncYears();
    if ("ResizeObserver" in window) {
      new ResizeObserver(() => syncYears()).observe(range);
    }
  });

  document.querySelectorAll("[data-file-input]").forEach((input) => {
    const label = input.closest("label");
    const name = label ? label.querySelector("[data-file-name]") : null;
    input.addEventListener("change", () => {
      if (name) {
        name.textContent = input.files && input.files.length ? input.files[0].name : "未选择文件";
      }
    });
  });

  document.querySelectorAll("[data-coach-api-settings]").forEach((settings) => {
    const fields = settings.querySelector("[data-coach-api-fields]");
    const radios = Array.from(settings.querySelectorAll("[name='agent_connection_mode']"));
    if (!fields || !radios.length) return;
    const controls = Array.from(fields.querySelectorAll("input, select, textarea, button"));
    const syncConnectionMode = () => {
      const expanded = radios.some((radio) => radio.checked && radio.value === "custom");
      fields.dataset.expanded = String(expanded);
      fields.setAttribute("aria-hidden", String(!expanded));
      controls.forEach((control) => {
        control.disabled = !expanded;
      });
    };
    radios.forEach((radio) => {
      radio.addEventListener("change", syncConnectionMode, { signal });
    });
    syncConnectionMode();
  });
}




export function initializeFilters(_signal, navigatePartial) {
  document.querySelectorAll("form.auto-filter").forEach((form) => {
    form.querySelector("[data-filter-reset]")?.addEventListener("click", () => {
      viewState.clearFilterState(form);
    });
    const autoApply = () => {
      rememberFilterState(form);
      const url = new URL(form.action || window.location.href, window.location.href);
      url.search = new URLSearchParams(new FormData(form)).toString();
      if (
        url.pathname === window.location.pathname
        && (url.pathname === "/" || url.pathname === "/papers")
      ) {
        fetchLibraryListUpdate(url);
      } else if (navigatePartial) {
        navigatePartial(url);
      } else {
        submitForm(form);
      }
    };
    // Filtering stays automatic (selecting a condition immediately updates the
    // list), but only the list region changes in place - the detail panel and
    // the rest of the page are never rebuilt or hidden.
    form.querySelectorAll("select").forEach((field) => {
      field.addEventListener("change", autoApply);
    });
    let timer = 0;
    form.querySelectorAll("input").forEach((field) => {
      field.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(autoApply, 400);
      });
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      autoApply();
    });
  });
}




export function initializeTabsAndPagination(signal, _navigatePartial) {
  document.querySelectorAll("[data-tabs]").forEach((tabset) => {
    const tabs = Array.from(tabset.querySelectorAll("[role='tab']"));
    const activate = (tab) => {
      tabs.forEach((candidate) => {
        const selected = candidate === tab;
        candidate.classList.toggle("active-tab", selected);
        candidate.setAttribute("aria-selected", selected ? "true" : "false");
        const panel = document.getElementById(candidate.dataset.tabTarget);
        if (panel) {
          panel.hidden = !selected;
          panel.classList.toggle("active-panel", selected);
        }
      });
    };
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => activate(tab));
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let nextIndex = index;
        if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
        if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
        if (event.key === "Home") nextIndex = 0;
        if (event.key === "End") nextIndex = tabs.length - 1;
        tabs[nextIndex].focus();
        activate(tabs[nextIndex]);
      });
    });
  });

  if (window.location.hash) {
    const hash = window.location.hash;
    let targetPanel = null;
    if (hash.startsWith("#material-")) {
      const num = hash.substring("#material-".length);
      targetPanel = document.querySelector(`[data-material-number="${num}"]`)?.closest('.tab-panel');
    } else if (hash.startsWith("#reference-")) {
      const idx = parseInt(hash.substring("#reference-".length)) - 1;
      targetPanel = document.querySelector(`[id$="-reference-panel-${idx}"]`);
    } else {
      targetPanel = document.getElementById(hash.substring(1));
    }

    if (targetPanel) {
      const tabset = targetPanel.closest('[data-tabs]');
      if (tabset) {
        const tabTargetId = targetPanel.id;
        const tab = tabset.querySelector(`[data-tab-target="${tabTargetId}"]`);
        if (tab) {
          setTimeout(() => {
            tab.click();
            const details = tab.closest('details');
            if (details) details.open = true;
            tab.scrollIntoView({ behavior: "smooth", block: "center" });
          }, 100);
        }
      }
    }
  }

  const paginatedGrid = document.querySelector("[data-adaptive-pagination]");
  if (paginatedGrid) {
    let resizeTimer = 0;
    const syncPagination = () => {
      const grid = document.querySelector("[data-adaptive-pagination]");
      if (!grid) return;
      const columns = getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean).length;
      const desiredSize = adaptivePageSize(columns);
      const currentSize = Number(grid.dataset.pageSize || 0);
      if (!desiredSize || desiredSize === currentSize) return;

      const currentPage = Number(grid.dataset.page || 1);
      const firstItemIndex = Math.max(0, (currentPage - 1) * currentSize);
      const nextPage = Math.floor(firstItemIndex / desiredSize) + 1;
      const url = new URL(window.location.href);
      url.searchParams.set("per_page", String(desiredSize));
      if (nextPage > 1) {
        url.searchParams.set("page", String(nextPage));
      } else {
        url.searchParams.delete("page");
      }
      fetchLibraryListUpdate(url);
    };

    syncPagination();
    window.addEventListener("resize", () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(syncPagination, 250);
    }, { signal: signal });
  }
}
