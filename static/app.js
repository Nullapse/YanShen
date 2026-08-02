import { initializeAgent } from "./js/agent.js";
import { initializeAnnotations } from "./js/annotations.js";
import {
  clearBrowserRecordState,
  configureStateNavigation,
  initializeDisplaySettings,
  initializeSessionNavigation,
  initializeSessionScrollState,
  initializeStartupRestoreSettings,
  initializeWorkflowMenus,
  viewState,
} from "./js/core.js";
import { initializeGrading } from "./js/grading.js";
import { initializeHome } from "./js/home.js";
import {
  configureNavigation,
  fetchLibraryListUpdate,
  initializePartialNavigation,
  navigatePartial,
  playPartialViewTransition,
} from "./js/navigation.js";
import { initializePractice } from "./js/practice.js";
import {
  initializeFilters,
  initializeGlobalForms,
  initializeShellControls,
  initializeTabsAndPagination,
} from "./js/shell.js";

export function mountPage() {
  const isPartialMount = window.__gongkaoPartialMount === true;
  window.__gongkaoPartialMount = false;
  if (!isPartialMount) {
    // A fresh page load (first entry / app restart) always starts with the
    // detail filter panel collapsed; only the active same-route filter
    // session keeps it open.
    viewState.sessionRemove(`filter-panel-open:${window.location.pathname}`);
  }
  window.__gongkaoPageAbortController?.abort();
  (window.__gongkaoPageIntervals || []).forEach((timer) => window.clearInterval(timer));
  window.__gongkaoPageIntervals = [];
  const pageAbortController = new AbortController();
  const signal = pageAbortController.signal;
  window.__gongkaoPageAbortController = pageAbortController;

  viewState.section = document.body?.dataset.activeSection || "";
  initializeSessionNavigation();
  initializeStartupRestoreSettings(signal);
  initializeSessionScrollState(signal);
  viewState.initializeDisclosureState();
  initializeWorkflowMenus(signal);
  initializeDisplaySettings();

  const clearLocalRecordMarker = document.querySelector("[data-clear-local-record-state]");
  if (clearLocalRecordMarker) {
    clearBrowserRecordState();
    clearLocalRecordMarker.remove();
  }

  initializeGlobalForms(signal);
  initializeAgent(signal, navigatePartial);
  initializeShellControls(signal);
  initializePractice(signal);
  initializeAnnotations(signal);
  initializeFilters(signal);
  initializeGrading(signal, navigatePartial);
  initializeTabsAndPagination(signal, navigatePartial);
  initializeHome(signal);
}

configureNavigation(mountPage);
configureStateNavigation((url, options = {}) => {
  const pathname = url.pathname;
  if (
    (pathname === "/" || pathname === "/papers")
    && pathname === window.location.pathname
  ) {
    // Restoring saved filter state only changes the list, so update it in
    // place instead of replaying the whole page entrance animation.
    fetchLibraryListUpdate(url);
    return;
  }
  navigatePartial(url, options);
});
document.addEventListener("DOMContentLoaded", () => {
  mountPage();
  initializePartialNavigation();
  playPartialViewTransition(document.querySelector(".main"));
}, { once: true });
