import { viewState } from "./core.js";

export const partialPageCache = new Map();
let mountPageCallback = null;

export function configureNavigation(callback) {
  mountPageCallback = callback;
}
export let partialNavigationSequence = 0;

const activeViewTransitionCleanup = new WeakMap();
const VIEW_STEP_SELECTOR = [
  ":scope > .section-tabs",
  ":scope > .page-head",
  ":scope > .workflow-header",
  ":scope > .library-toolbar",
  ":scope > .library-filter-panel:not([hidden])",
  ":scope > .records-filter",
  ":scope > .view-tabs",
  ":scope > .filters",
  ":scope > .coverage-filter",
  ":scope > .import-panel",
  ":scope > .home-welcome",
  ":scope > .home-focus-grid",
  ":scope > .home-quick-section",
  ":scope > .home-lower-grid",
  ":scope > .question-grid > .question-card",
  ":scope > .favorites-grid > .question-card",
  ":scope > .record-list > .record-card",
  ":scope > .settings-layout > .settings-column",
  ":scope > .settings-grid > *",
  ":scope > .paper-workspace > *",
  ":scope > .answer-workspace > *",
  ":scope > .grading-layout > *",
  ":scope > .agent-workspace > *",
  ":scope > .statistics-compact > *",
  ":scope > .pagination",
  ":scope > .empty-state",
].join(",");

export function partialViewTransitionSteps(root) {
  if (!root) return [];
  const steps = Array.from(root.querySelectorAll(VIEW_STEP_SELECTOR)).filter(
    (element) => element.getClientRects().length > 0,
  );
  if (steps.length) return steps;
  return Array.from(root.children).filter((element) => (
    !element.hidden
    && !element.matches("script, style, .flash-stack")
    && element.getClientRects().length > 0
  ));
}

export function playPartialViewTransition(root, { silent = false, staged = false } = {}) {
  if (!root) return;
  activeViewTransitionCleanup.get(root)?.(false);
  root.classList.remove("is-view-entering");
  if (silent || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    delete document.body.dataset.navigationMotion;
    return;
  }

  const steps = partialViewTransitionSteps(root);
  if (!steps.length) {
    delete document.body.dataset.navigationMotion;
    return;
  }
  steps.forEach((element) => {
    element.classList.add("is-view-step-entering");
  });
  steps.forEach((element, index) => {
    element.style.setProperty("--view-step-delay", `${Math.min(index, 7) * 28}ms`);
  });
  let cleaned = false;
  let animationFrame = 0;
  const cleanup = (clearMotion = true) => {
    if (cleaned) return;
    cleaned = true;
    window.cancelAnimationFrame(animationFrame);
    root.removeEventListener("animationend", onFinished);
    root.removeEventListener("animationcancel", onFinished);
    root.classList.remove("is-view-preparing");
    root.classList.remove("is-view-entering");
    steps.forEach((element) => {
      element.classList.remove("is-view-step-entering");
      element.style.removeProperty("--view-step-delay");
    });
    activeViewTransitionCleanup.delete(root);
    if (clearMotion) delete document.body.dataset.navigationMotion;
  };
  const onFinished = (event) => {
    if (event.target === steps.at(-1) && event.animationName === "ui-view-step-arrive") cleanup();
  };
  root.addEventListener("animationend", onFinished);
  root.addEventListener("animationcancel", onFinished);
  activeViewTransitionCleanup.set(root, cleanup);
  const begin = () => {
    if (cleaned) return;
    root.classList.remove("is-view-preparing");
    void root.offsetWidth;
    root.classList.add("is-view-entering");
  };
  if (staged) {
    // WebView2 can coalesce the first cross-section DOM replacement and CSS
    // animation into one paint. Hold the prepared first frame only for the
    // home -> paper-library entry so its upward motion is always visible.
    root.classList.add("is-view-preparing");
    animationFrame = window.requestAnimationFrame(begin);
  } else {
    begin();
  }
}

export function partialNavigationUrl(element, overrideUrl = "") {
  const raw = overrideUrl || (element instanceof HTMLFormElement
    ? (element.action || window.location.href)
    : element?.href);
  if (!raw) return null;
  const url = new URL(raw, window.location.href);
  if (url.origin !== window.location.origin) return null;
  if (
    url.pathname.startsWith("/static/")
    || url.pathname.startsWith("/templates/")
    || url.pathname === "/settings/export"
    || /\.(?:csv|json|md|zip|pdf)$/i.test(url.pathname)
  ) return null;
  if (element instanceof HTMLFormElement) {
    if (
      element.target
      || element.dataset.noPartialNavigation === "1"
      || (element.enctype || "").toLowerCase() === "multipart/form-data"
    ) return null;
  }
  if (element instanceof HTMLAnchorElement) {
    if (
      element.target
      || element.hasAttribute("download")
      || element.closest("[data-no-partial-navigation]")
      || (url.pathname === window.location.pathname
        && url.search === window.location.search
        && url.hash)
    ) return null;
  }
  const filterSections = {
    "/": {
      section: "index",
      fields: ["exam_type", "region", "question_type", "work_status", "year_from", "year_to", "organization", "q", "sort_refs"],
    },
    "/papers": {
      section: "papers",
      fields: ["exam_type", "region", "paper_category", "work_status", "year_from", "year_to", "q", "sort_refs"],
    },
  };
  const filterConfig = filterSections[url.pathname];
  if (filterConfig) {
    // Merge the saved filter state into the target URL up front so the first
    // render is already filtered - otherwise restoring saved filters causes a
    // visible second list update right after entering the page.
    const saved = viewState.persistentGet(`filters:${filterConfig.section}:${url.pathname}`);
    if (saved) {
      const savedParams = new URLSearchParams(saved);
      filterConfig.fields.forEach((name) => url.searchParams.delete(name));
      savedParams.forEach((value, name) => url.searchParams.append(name, value));
    }
  }
  if (
    (url.pathname === "/" || url.pathname === "/papers")
    && url.pathname === window.location.pathname
  ) {
    applyAdaptivePageSize(url);
  }
  return url;
}

export function fetchPartialDocument(url, { useCache = true, requestInit = {} } = {}) {
  const key = url.href;
  const method = String(requestInit.method || "GET").toUpperCase();
  const cacheable = useCache && method === "GET";
  const cached = partialPageCache.get(key);
  if (cacheable && cached && Date.now() - cached.createdAt < 12000) {
    return cached.promise;
  }
  const promise = fetch(key, {
    ...requestInit,
    headers: {
      ...(requestInit.headers || {}),
      "Accept": "text/html",
      "X-Gongkao-Navigation": "partial",
    },
    credentials: "same-origin",
  }).then(async (response) => {
    if (!response.ok) throw new Error(`navigation failed: ${response.status}`);
    const text = await response.text();
    return {
      document: new DOMParser().parseFromString(text, "text/html"),
      url: new URL(response.url || url.href, window.location.href),
    };
  });
  if (cacheable) {
    if (partialPageCache.size >= 30) {
      partialPageCache.delete(partialPageCache.keys().next().value);
    }
    partialPageCache.set(key, { createdAt: Date.now(), promise });
    promise.catch(() => partialPageCache.delete(key));
  }
  return promise;
}

export async function fetchLibraryListUpdate(url) {
  const response = await fetch(url.href, {
    headers: {
      "Accept": "text/html",
      "X-Gongkao-List-Partial": "1",
    },
    credentials: "same-origin",
  });
  if (!response.ok) throw new Error(`list update failed: ${response.status}`);
  const text = await response.text();
  const parsed = new DOMParser().parseFromString(text, "text/html");
  const partial = parsed.querySelector("[data-list-partial]");
  if (!partial) throw new Error("library list response is missing its partial root");

  const metrics = partial.querySelector(".metrics");
  const currentMetrics = document.querySelector(".page-head .metrics");
  if (metrics && currentMetrics) {
    currentMetrics.replaceWith(metrics);
  }

  const nextGrid = partial.querySelector("[data-adaptive-pagination]");
  const currentGrid = document.querySelector("[data-adaptive-pagination]");
  if (nextGrid && currentGrid) {
    currentGrid.replaceWith(nextGrid);
  }

  const nextPager = partial.querySelector(".pagination");
  const currentPager = document.querySelector(".main .pagination");
  if (nextPager && currentPager) {
    currentPager.replaceWith(nextPager);
  } else if (nextPager) {
    currentGrid?.insertAdjacentElement("afterend", nextPager);
  } else if (currentPager) {
    currentPager.remove();
  }

  window.dispatchEvent(new CustomEvent("gongkao:library-list-updated", { detail: { url: url.href } }));
  window.history.replaceState({}, "", url.href);
  return true;
}

export function sidebarNavigationGroup(pathname) {
  if (pathname === "/" || pathname.startsWith("/papers") || pathname.startsWith("/favorites")) return "library";
  if (pathname.startsWith("/attempts") || pathname.startsWith("/notes") || pathname.startsWith("/statistics")) return "learning";
  return "";
}

export function syncSidebarNavigation(currentNav, nextNav) {
  const nextAnchors = Array.from(nextNav.querySelectorAll("a[href]"));
  currentNav.querySelectorAll("a[href]").forEach((anchor) => {
    // The first matching anchor for an href may be a top-level link (e.g. 题库
    // and 试卷题库 both point at /papers). Only sync anchors with the same
    // submenu context so the active state moves to the right label.
    const inSubmenu = Boolean(anchor.closest(".nav-submenu"));
    const replacement = nextAnchors.find((candidate) => (
      candidate.getAttribute("href") === anchor.getAttribute("href")
      && Boolean(candidate.closest(".nav-submenu")) === inSubmenu
    ));
    if (replacement) anchor.className = replacement.className;
  });
  const currentClusters = Array.from(currentNav.querySelectorAll(".nav-cluster"));
  const nextClusters = Array.from(nextNav.querySelectorAll(".nav-cluster"));
  currentClusters.forEach((cluster, index) => {
    if (!nextClusters[index]) return;
    const nextCluster = nextClusters[index];
    cluster.className = nextCluster.className;
    const currentSubmenu = cluster.querySelector(":scope > .nav-submenu");
    const nextSubmenu = nextCluster.querySelector(":scope > .nav-submenu");
    if (!currentSubmenu && nextSubmenu) {
      cluster.append(nextSubmenu.cloneNode(true));
    } else if (currentSubmenu && !nextSubmenu) {
      currentSubmenu.remove();
    }
  });
  const currentWeek = currentNav.querySelector(":scope > .home-week-card");
  const nextWeek = nextNav.querySelector(":scope > .home-week-card");
  if (currentWeek && nextWeek) {
    currentWeek.replaceWith(nextWeek.cloneNode(true));
  } else if (!currentWeek && nextWeek) {
    currentNav.querySelector(":scope > .nav-settings")?.before(nextWeek.cloneNode(true));
  } else {
    currentWeek?.remove();
  }
}

function adaptiveLibraryPageSize() {
  const grid = document.querySelector("[data-adaptive-pagination]");
  let columns = 0;
  if (grid) {
    columns = getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean).length;
  }
  if (!columns) {
    const main = document.querySelector(".main");
    const width = main ? main.clientWidth : window.innerWidth;
    // Library pages reserve the sidebar plus main padding; estimate the grid
    // content width instead of counting the whole main element as one column.
    const contentWidth = Math.max(280, width - 270 - 64);
    columns = Math.max(1, Math.floor((contentWidth + 14) / (310 + 14)));
  }
  if (columns <= 1) return 10;
  if (columns === 2) return 12;
  return columns * 4;
}

function applyAdaptivePageSize(url) {
  if (url.pathname !== "/" && url.pathname !== "/papers") return;
  const size = adaptiveLibraryPageSize();
  if (size) url.searchParams.set("per_page", String(size));
}

export async function navigatePartial(
  url,
  { replace = false, fromPopState = false, requestInit = {}, silent = false } = {},
) {
  const sequence = ++partialNavigationSequence;
  const method = String(requestInit.method || "GET").toUpperCase();
  const currentMain = document.querySelector(".main");
  if (!currentMain) {
    window.location.assign(url.href);
    return;
  }
  const previousSection = document.body.dataset.activeSection || "";
  const currentNavigationGroup = sidebarNavigationGroup(window.location.pathname);
  const nextNavigationGroup = sidebarNavigationGroup(url.pathname);
  const stableSubnavUpdate = Boolean(currentNavigationGroup && currentNavigationGroup === nextNavigationGroup);
  if (!stableSubnavUpdate) currentMain.classList.add("is-partial-loading");
  currentMain.setAttribute("aria-busy", "true");
  try {
    const response = await fetchPartialDocument(url, {
      useCache: method === "GET",
      requestInit,
    });
    if (sequence !== partialNavigationSequence) return;
    const nextDocument = response.document;
    const resolvedUrl = response.url;
    const nextMain = nextDocument.querySelector(".main");
    const nextNav = nextDocument.querySelector(".sidebar .nav");
    if (!nextMain || !nextNav) throw new Error("partial navigation shell missing");

    const nextSection = nextDocument.body?.dataset.activeSection || "";
    const internalUpdate = previousSection === nextSection || stableSubnavUpdate;
    const keepScroll = internalUpdate && (
      /^\/papers\/\d+\/?$/.test(window.location.pathname)
      || /^\/attempts\/\d+\/?$/.test(window.location.pathname)
      || previousSection === "agent"
    );
    const currentRail = currentMain.querySelector(".agent-thread-rail");
    const nextRail = nextMain.querySelector(".agent-thread-rail");
    const preserveRail = Boolean(
      currentRail
      && nextRail
      && method === "GET"
      && previousSection === "agent"
      && nextSection === "agent"
    );
    if (preserveRail) currentRail.remove();
    window.dispatchEvent(new CustomEvent("gongkao:before-navigation", { detail: { url: resolvedUrl.href } }));
    document.body.dataset.navigationMotion = silent ? "silent" : (internalUpdate ? "internal" : "section");
    document.body.dataset.activeSection = nextSection;
    if (nextDocument.body?.dataset.transientRoute === "1") {
      document.body.dataset.transientRoute = "1";
    } else {
      delete document.body.dataset.transientRoute;
    }
    document.title = nextDocument.title;
    const currentNav = document.querySelector(".sidebar .nav");
    if (!silent) {
      // Silent replaces (grading submissions, pagination corrections) must not
      // re-render the sidebar; only the content area changes.
      syncSidebarNavigation(currentNav, nextNav);
    }
    currentMain.innerHTML = nextMain.innerHTML;
    if (preserveRail) {
      // innerHTML assignment clones the fetched markup, so the parsed nextRail
      // node is detached. Remove the freshly inserted rail copy before
      // restoring the preserved one to avoid duplicate thread lists.
      currentMain.querySelector(".agent-thread-rail")?.remove();
      const workspace = currentMain.querySelector(".agent-workspace");
      if (workspace) {
        const nextActiveHrefs = new Set(
          Array.from(nextRail.querySelectorAll(".agent-thread-row.active a[href]")).map(
            (anchor) => anchor.getAttribute("href"),
          ),
        );
        currentRail.querySelectorAll(".agent-thread-row").forEach((row) => {
          const href = row.querySelector("a[href]")?.getAttribute("href") || "";
          row.classList.toggle("active", nextActiveHrefs.has(href));
        });
        workspace.prepend(currentRail);
      } else {
        currentMain.append(currentRail);
      }
    }
    if (!fromPopState) {
      window.history[replace ? "replaceState" : "pushState"]({}, "", resolvedUrl.href);
    }
    partialPageCache.delete(url.href);
    window.__gongkaoPartialMount = true;
    mountPageCallback?.();
    if (!keepScroll) window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    playPartialViewTransition(currentMain, {
      silent,
      staged: previousSection === "home" && nextSection === "papers",
    });
    window.requestAnimationFrame(() => {
      currentMain.classList.remove("is-partial-loading");
      currentMain.removeAttribute("aria-busy");
    });
  } catch (error) {
    if (sequence !== partialNavigationSequence) return;
    console.warn("Partial navigation fallback:", error);
    if (method === "GET") {
      window.location.assign(url.href);
    } else {
      window.location.reload();
    }
  }
}

function formRequestBody(form, submitter) {
  const formData = new FormData(form);
  if (submitter?.name && !formData.has(submitter.name)) {
    formData.append(submitter.name, submitter.value || "");
  }
  const body = new URLSearchParams();
  for (const [name, value] of formData.entries()) {
    if (typeof value !== "string") return null;
    body.append(name, value);
  }
  return body;
}

export function initializePartialNavigation() {
  if (document.documentElement.dataset.partialNavigationReady === "1") return;
  document.documentElement.dataset.partialNavigationReady = "1";

  document.addEventListener("click", (event) => {
    if (
      event.defaultPrevented
      || event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) return;
    const anchor = event.target.closest("a[href]");
    if (!anchor) return;
    const url = partialNavigationUrl(anchor);
    if (!url) return;
    const isListUpdate = (
      (url.pathname === "/" || url.pathname === "/papers")
      && window.location.pathname === url.pathname
      && Boolean(anchor.closest(".pagination"))
    );
    if (isListUpdate) {
      event.preventDefault();
      fetchLibraryListUpdate(url);
      return;
    }
    event.preventDefault();
    navigatePartial(url);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || event.defaultPrevented) return;
    const submitter = event.submitter;
    const method = (
      submitter?.getAttribute("formmethod")
      || form.getAttribute("method")
      || "get"
    ).toLowerCase();
    if (!["get", "post"].includes(method)) return;
    const action = submitter?.getAttribute("formaction") || form.action;
    const url = partialNavigationUrl(form, action);
    if (!url) return;
    const data = new FormData(form);
    if (method === "get") {
      url.search = new URLSearchParams(data).toString();
      if ((url.pathname === "/" || url.pathname === "/papers") && window.location.pathname === url.pathname) {
        event.preventDefault();
        fetchLibraryListUpdate(url);
        return;
      }
      event.preventDefault();
      navigatePartial(url);
      return;
    }
    const body = formRequestBody(form, submitter);
    if (!body) return;
    event.preventDefault();
    const silent = Boolean(
      form.hasAttribute("data-preserve-sidebar")
      || submitter?.hasAttribute("data-grade-submit"),
    );
    navigatePartial(url, {
      requestInit: {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        body,
      },
      silent,
    });
  });

  let prefetchTimer = 0;
  document.addEventListener("pointerover", (event) => {
    const anchor = event.target.closest("a[href]");
    const url = anchor ? partialNavigationUrl(anchor) : null;
    if (!url) return;
    // After a navigation swaps the sidebar submenu, the stationary pointer can
    // land on the newly inserted link for the page that just finished loading.
    // Prefetching that exact URL creates a redundant second request and makes
    // the transition look like a double refresh in WebView.
    if (url.href === window.location.href) return;
    window.clearTimeout(prefetchTimer);
    prefetchTimer = window.setTimeout(() => {
      if (url.href === window.location.href) return;
      fetchPartialDocument(url).catch(() => {});
    }, 90);
  }, { passive: true });

  window.addEventListener("popstate", () => {
    navigatePartial(new URL(window.location.href), { fromPopState: true });
  });
}
