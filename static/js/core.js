export function submitForm(form) {
  if (form.dataset.submitting === "1") return;
  form.dataset.submitting = "1";
  if (form.requestSubmit) {
    form.requestSubmit();
  } else {
    form.submit();
  }
}

export function adaptivePageSize(columnCount) {
  if (columnCount <= 1) return 10;
  if (columnCount === 2) return 12;
  return columnCount * 4;
}

export function countAnswerCharacters(value) {
  return answerGridMetrics(value).occupiedCells;
}

export function answerLineCount(characterCount) {
  return Math.ceil(Math.max(0, Number(characterCount) || 0) / 25);
}

export function answerGridCellsForLine(value) {
  const characters = Array.from(value || "");
  let cells = 0;
  let index = 0;
  while (index < characters.length) {
    const character = characters[index];
    if (character === "—" || character === "…") {
      if (characters[index + 1] === character) index += 1;
      cells += 2;
      index += 1;
      continue;
    }
    if (/^[A-Za-z0-9]$/.test(character)) {
      let end = index + 1;
      while (end < characters.length && /^[A-Za-z0-9]$/.test(characters[end])) end += 1;
      cells += Math.ceil((end - index) / 2);
      index = end;
      continue;
    }
    // 汉字、全角标点、半角符号和实际输入的空格均占一格。
    cells += 1;
    index += 1;
  }
  return cells;
}

export function answerGridMetrics(value) {
  const logicalLines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  let occupiedCells = 0;
  let lines = 0;
  let currentLineCells = 0;
  logicalLines.forEach((line, index) => {
    if (index < logicalLines.length - 1 && line === "") return;
    const contentCells = answerGridCellsForLine(line);
    currentLineCells = contentCells ? ((contentCells - 1) % 25) + 1 : 0;
    if (index < logicalLines.length - 1) {
      const lineCount = Math.ceil(contentCells / 25);
      occupiedCells += lineCount * 25;
      lines += lineCount;
    } else {
      occupiedCells += contentCells;
      lines += Math.ceil(contentCells / 25);
    }
  });
  return { occupiedCells, lines, currentLineCells };
}

export function answerLimit(value) {
  const compact = String(value || "").replace(/\s+/g, "");
  const numbers = compact.match(/\d+/g);
  if (!numbers || !numbers.length) return 0;
  const upper = Math.max(...numbers.map(Number));
  if (/(不少于|不低于|至少|以上)/.test(compact)) return 0;
  if (compact.includes("左右") && upper > 500) return 0;
  return upper;
}

export function clampNumber(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

export const VIEW_STATE_NAMESPACE = "gongkao.viewState.v2";

export class ViewStateManager {
  constructor(namespace = VIEW_STATE_NAMESPACE) {
    this.namespace = namespace;
    this.section = document.body?.dataset.activeSection || "";
  }

  storageKey(key) {
    return `${this.namespace}:${key}`;
  }

  read(storage, key, migrateLegacy = true) {
    try {
      const namespacedKey = this.storageKey(key);
      const current = storage.getItem(namespacedKey);
      if (current !== null) return current;
      if (!migrateLegacy) return null;
      const legacy = storage.getItem(key);
      if (legacy === null) return null;
      storage.setItem(namespacedKey, legacy);
      storage.removeItem(key);
      return legacy;
    } catch (error) {
      return null;
    }
  }

  write(storage, key, value) {
    try {
      storage.setItem(this.storageKey(key), String(value));
      return true;
    } catch (error) {
      return false;
    }
  }

  remove(storage, key) {
    try {
      storage.removeItem(this.storageKey(key));
      storage.removeItem(key);
    } catch (error) {
      // State persistence is an enhancement; the application remains usable without it.
    }
  }

  sessionGet(key) {
    return this.read(sessionStorage, key);
  }

  sessionSet(key, value) {
    return this.write(sessionStorage, key, value);
  }

  sessionRemove(key) {
    this.remove(sessionStorage, key);
  }

  persistentGet(key) {
    return this.read(localStorage, key);
  }

  persistentSet(key, value) {
    return this.write(localStorage, key, value);
  }

  persistentRemove(key) {
    this.remove(localStorage, key);
  }

  canonicalUrl(value = window.location.href, { keepHash = true } = {}) {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin) return "";
    ["return_to", "grading_job", "smart", "deep"].forEach((name) => url.searchParams.delete(name));
    const query = url.searchParams.toString();
    return `${url.pathname}${query ? `?${query}` : ""}${keepHash ? url.hash : ""}`;
  }

  pageKey() {
    return this.canonicalUrl(window.location.href, { keepHash: false });
  }

  filterKey(form) {
    return `filters:${this.section || "page"}:${form.dataset.stateKey || window.location.pathname}`;
  }

  initializeScrollState(signal) {
    const pageKey = this.pageKey();
    const windowStorageKey = `scroll:${pageKey}:window`;
    const restorePreferences = readStartupRestorePreferences();
    const readScroll = (key) => (
      restorePreferences.restoreScroll ? this.persistentGet(key) : this.sessionGet(key)
    );
    const writeScroll = (key, value) => (
      restorePreferences.restoreScroll ? this.persistentSet(key, value) : this.sessionSet(key, value)
    );
    const savedWindowScroll = Number(readScroll(windowStorageKey) || 0);
    if (savedWindowScroll > 0) {
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => window.scrollTo(0, savedWindowScroll)));
    }
    const scrollContainers = Array.from(document.querySelectorAll("[data-session-scroll]"));
    scrollContainers.forEach((container) => {
      const key = `scroll:${pageKey}:${container.dataset.sessionScroll}`;
      const saved = Number(readScroll(key) || 0);
      if (saved > 0) container.scrollTop = saved;
    });
    const persistScrollState = () => {
      writeScroll(windowStorageKey, window.scrollY);
      scrollContainers.forEach((container) => {
        writeScroll(`scroll:${pageKey}:${container.dataset.sessionScroll}`, container.scrollTop);
      });
    };
    window.addEventListener("pagehide", persistScrollState, signal ? { signal } : undefined);
    window.addEventListener("gongkao:before-navigation", persistScrollState, signal ? { signal } : undefined);
  }

  initializeDisclosureState() {
    const pageKey = this.pageKey();
    document.querySelectorAll("main details:not([data-workflow-menu])").forEach((details, index) => {
      const identity = details.dataset.stateKey || details.id || `${details.className || "details"}:${index}`;
      const key = `disclosure:${pageKey}:${identity}`;
      const saved = this.sessionGet(key);
      if (details.dataset.defaultOpen === "1") {
        details.open = true;
      } else if (saved !== null) {
        details.open = saved === "1";
      }
      details.addEventListener("toggle", () => this.sessionSet(key, details.open ? "1" : "0"));
    });
  }

  clearFilterState(form) {
    this.persistentRemove(this.filterKey(form));
    this.sessionRemove(this.filterKey(form));
  }
}

export const viewState = new ViewStateManager();
let stateNavigator = null;
export const STARTUP_RESTORE_PREFERENCES_KEY = "startup-restore-preferences";
export const STARTUP_LAST_ROUTE_KEY = "startup:last-route";

export function configureStateNavigation(callback) {
  stateNavigator = callback;
}

export function readStartupRestorePreferences() {
  try {
    const parsed = JSON.parse(viewState.persistentGet(STARTUP_RESTORE_PREFERENCES_KEY) || "{}");
    return {
      restoreLastPage: parsed.restoreLastPage !== false,
      restoreScroll: parsed.restoreScroll !== false,
    };
  } catch (error) {
    return { restoreLastPage: true, restoreScroll: true };
  }
}

export function writeStartupRestorePreferences(preferences) {
  viewState.persistentSet(STARTUP_RESTORE_PREFERENCES_KEY, JSON.stringify(preferences));
}

export function initializeStartupRestoreSettings(signal) {
  if (document.body?.dataset.transientRoute !== "1") {
    const route = viewState.canonicalUrl();
    if (route && route !== "/settings/export") {
      viewState.persistentSet(STARTUP_LAST_ROUTE_KEY, route);
    }
  }

  const preferences = readStartupRestorePreferences();
  document.querySelectorAll("[data-startup-restore]").forEach((checkbox) => {
    const preferenceName = checkbox.dataset.startupRestore;
    checkbox.checked = preferenceName === "last-page"
      ? preferences.restoreLastPage
      : preferences.restoreScroll;
    checkbox.addEventListener("change", () => {
      const next = readStartupRestorePreferences();
      if (preferenceName === "last-page") next.restoreLastPage = checkbox.checked;
      if (preferenceName === "scroll") next.restoreScroll = checkbox.checked;
      writeStartupRestorePreferences(next);
    }, signal ? { signal } : undefined);
  });
}

export function readPaneWidth(storageKey) {
  const stored = Number(viewState.persistentGet(storageKey) || 0);
  if (Number.isFinite(stored) && stored > 0) return stored;
  // One-time migration from the cookie used by v1.2.6 and early v1.3.0 builds.
  const cookieKey = encodeURIComponent(storageKey);
  try {
    const prefix = `${cookieKey}=`;
    const match = document.cookie.split("; ").find((item) => item.startsWith(prefix));
    if (match) {
      const value = Number(decodeURIComponent(match.slice(prefix.length)));
      if (Number.isFinite(value) && value > 0) {
        viewState.persistentSet(storageKey, value);
        document.cookie = `${cookieKey}=; Path=/; Max-Age=0; SameSite=Lax`;
        return value;
      }
    }
  } catch (error) {
    // Ignore a malformed legacy cookie.
  }
  return 0;
}

export function writePaneWidth(storageKey, width) {
  const value = String(Math.round(width));
  viewState.persistentSet(storageKey, value);
}

export function readSessionState(key) {
  return viewState.sessionGet(key);
}

export function writeSessionState(key, value) {
  return viewState.sessionSet(key, value);
}

export function sessionFilterState(form) {
  const params = new URLSearchParams();
  new FormData(form).forEach((value, key) => {
    if (key === "per_page") return;
    const text = String(value).trim();
    if (text) params.append(key, text);
  });
  return params.toString();
}

export function restoreFilterState(form) {
  const storageKey = viewState.filterKey(form);
  const fieldNames = new Set(Array.from(form.elements).map((field) => field.name).filter(Boolean));
  const currentParams = new URLSearchParams(window.location.search);
  const hasExplicitFilters = Array.from(fieldNames).some(
    (name) => name !== "per_page" && currentParams.has(name),
  );
  let savedState = viewState.persistentGet(storageKey);
  if (savedState === null) {
    savedState = readSessionState(storageKey);
    if (savedState !== null) {
      viewState.persistentSet(storageKey, savedState);
      viewState.sessionRemove(storageKey);
    }
  }

  if (!hasExplicitFilters && savedState !== null) {
    const savedParams = new URLSearchParams(savedState);
    savedParams.delete("per_page");
    fieldNames.forEach((name) => currentParams.delete(name));
    currentParams.delete("page");
    savedParams.forEach((value, name) => currentParams.append(name, value));
    const query = currentParams.toString();
    const target = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
    const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (target !== current) {
      stateNavigator?.(new URL(target, window.location.origin), { replace: true });
      return true;
    }
  } else if (hasExplicitFilters) {
    viewState.persistentSet(storageKey, sessionFilterState(form));
  }
  return false;
}

export function rememberFilterState(form) {
  viewState.persistentSet(viewState.filterKey(form), sessionFilterState(form));
}

export function initializeSessionNavigation() {
  // Workflow links are deliberately server-defined. Session state is limited
  // to filters, drafts, timers, scroll positions and disclosure preferences.
}

export function initializeSessionScrollState(signal) {
  viewState.initializeScrollState(signal);
}

export function draftStorageKey(form) {
  return `gongkao.answerDraft:${new URL(form.action, window.location.href).pathname}`;
}

export function readDraft(key) {
  return viewState.persistentGet(key);
}

export function writeDraft(key, value) {
  return viewState.persistentSet(key, value);
}

export function removeDraft(key) {
  viewState.persistentRemove(key);
}

export function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function paragraphHtml(value) {
  return escapeHtml(value)
    .split(/\n{2,}/)
    .map((part) => `<p>${part.replace(/\n/g, "<br>")}</p>`)
    .join("");
}

export let currentConfirmCallback = null;
export function getOrCreateConfirmModal() {
  let modal = document.getElementById("custom-confirm-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.className = "annotation-modal-overlay";
    modal.id = "custom-confirm-modal";
    modal.innerHTML = `
      <div class="annotation-modal-card confirm-modal-card">
        <div class="annotation-modal-header">提示</div>
        <div class="confirm-modal-message" style="margin: 8px 0 20px; color: var(--text); font-size: 14px; line-height: 1.5;"></div>
        <div class="annotation-modal-footer" style="justify-content: flex-end;">
          <div class="annotation-modal-actions">
            <button type="button" class="annotation-modal-btn cancel">取消</button>
            <button type="button" class="annotation-modal-btn primary confirm">确定</button>
          </div>
        </div>
      </div>
    `;
    document.body.append(modal);

    const closeModal = () => {
      modal.classList.remove("active");
      currentConfirmCallback = null;
    };

    modal.querySelector(".cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal();
    });

    modal.querySelector(".confirm").addEventListener("click", () => {
      if (currentConfirmCallback) {
        currentConfirmCallback();
      }
      closeModal();
    });

    window.addEventListener("keydown", (event) => {
      if (!modal.classList.contains("active")) return;
      if (event.key === "Escape") {
        closeModal();
      } else if (event.key === "Enter") {
        event.preventDefault();
        modal.querySelector(".confirm").click();
      }
    });
  }
  return modal;
}

export function showConfirmModal(message, onConfirm) {
  const modal = getOrCreateConfirmModal();
  modal.querySelector(".confirm-modal-message").textContent = message;
  currentConfirmCallback = onConfirm;
  modal.classList.add("active");
  window.setTimeout(() => {
    modal.querySelector(".confirm").focus();
  }, 50);
}

export function showUndoToast(message, onUndo) {
  document.querySelector("[data-undo-toast]")?.remove();
  const toast = document.createElement("div");
  toast.className = "undo-toast";
  toast.dataset.undoToast = "1";
  toast.setAttribute("role", "status");
  toast.innerHTML = `<span>${escapeHtml(message)}</span><button type="button">撤回</button>`;
  let active = true;
  const dismiss = () => {
    active = false;
    toast.remove();
  };
  const timer = window.setTimeout(dismiss, 8000);
  toast.querySelector("button")?.addEventListener("click", () => {
    if (!active) return;
    window.clearTimeout(timer);
    onUndo();
    dismiss();
  });
  document.body.appendChild(toast);
}

export function clearBrowserRecordState() {
  try {
    Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index))
      .filter(Boolean)
      .filter((key) => key.startsWith(`${VIEW_STATE_NAMESPACE}:`) || key === "gongkao.home-plan.v3")
      .forEach((key) => localStorage.removeItem(key));
    Array.from({ length: sessionStorage.length }, (_, index) => sessionStorage.key(index))
      .filter(Boolean)
      .filter((key) => key.startsWith(`${VIEW_STATE_NAMESPACE}:`) || key.startsWith("filters:"))
      .forEach((key) => sessionStorage.removeItem(key));
  } catch (error) {
    // The database cleanup remains valid when browser storage is unavailable.
  }
}

export const DISPLAY_PREFERENCES_KEY = "display-preferences";
export const UI_ZOOM_MIN = 0.5;
export const UI_ZOOM_MAX = 2;
export const UI_ZOOM_STEP = 0.1;

export function normalizedUiZoom(value) {
  return Math.round(clampNumber(Number(value) || defaultUiZoom(), UI_ZOOM_MIN, UI_ZOOM_MAX) * 10) / 10;
}

export function defaultUiZoom() {
  return Math.min(window.screen.width, window.screen.height) <= 1080 ? 0.9 : 1;
}

export function readDisplayPreferences() {
  try {
    const parsed = JSON.parse(viewState.persistentGet(DISPLAY_PREFERENCES_KEY) || "{}");
    return {
      profile: ["fullscreen", "window"].includes(parsed.profile) ? parsed.profile : "",
      zoom: Object.prototype.hasOwnProperty.call(parsed, "zoom") ? normalizedUiZoom(parsed.zoom) : defaultUiZoom(),
    };
  } catch (error) {
    return { profile: "", zoom: defaultUiZoom() };
  }
}

export function writeDisplayPreferences(preferences) {
  viewState.persistentSet(DISPLAY_PREFERENCES_KEY, JSON.stringify(preferences));
}

export function sendDesktopDisplayCommand(command) {
  if (window.chrome?.webview?.postMessage) {
    window.chrome.webview.postMessage(command);
    return true;
  }
  return false;
}

export function applyUiZoom(zoom) {
  const normalized = normalizedUiZoom(zoom);
  if (!sendDesktopDisplayCommand(`zoom:${normalized}`)) {
    document.documentElement.style.zoom = String(normalized);
  }
}

export function applyDisplayProfile(profile) {
  if (!profile) return;
  sendDesktopDisplayCommand(`display:${profile}`);
}

export function syncDisplaySettingControls(preferences = readDisplayPreferences()) {
  document.querySelectorAll("[data-display-profile]").forEach((button) => {
    const active = button.dataset.displayProfile === preferences.profile;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  document.querySelectorAll("[data-ui-zoom-value]").forEach((output) => {
    output.textContent = `${Math.round(preferences.zoom * 100)}%`;
  });
}

export function initializeDisplaySettings() {
  const preferences = readDisplayPreferences();
  if (document.documentElement.dataset.displayPreferencesApplied !== "1") {
    document.documentElement.dataset.displayPreferencesApplied = "1";
    applyUiZoom(preferences.zoom);
    if (preferences.profile === "fullscreen") applyDisplayProfile("fullscreen");
  }
  syncDisplaySettingControls(preferences);

  document.querySelectorAll("[data-display-profile]").forEach((button) => {
    button.addEventListener("click", () => {
      const next = readDisplayPreferences();
      next.profile = button.dataset.displayProfile || "window";
      writeDisplayPreferences(next);
      applyDisplayProfile(next.profile);
      applyUiZoom(next.zoom);
      syncDisplaySettingControls(next);
    });
  });

  document.querySelectorAll("[data-ui-zoom-adjust]").forEach((button) => {
    button.addEventListener("click", () => {
      const next = readDisplayPreferences();
      next.zoom = normalizedUiZoom(next.zoom + Number(button.dataset.uiZoomAdjust) * UI_ZOOM_STEP);
      writeDisplayPreferences(next);
      applyUiZoom(next.zoom);
      syncDisplaySettingControls(next);
    });
  });

  if (document.documentElement.dataset.displayShortcutsReady === "1") return;
  document.documentElement.dataset.displayShortcutsReady = "1";
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      const next = readDisplayPreferences();
      if (next.profile !== "fullscreen") return;
      event.preventDefault();
      next.profile = "window";
      writeDisplayPreferences(next);
      applyDisplayProfile("window");
      syncDisplaySettingControls(next);
      return;
    }
    if (!event.ctrlKey || event.altKey || event.metaKey) return;
    const isReset = event.key === "0";
    const isIncrease = event.key === "+" || event.key === "=";
    const isDecrease = event.key === "-" || event.key === "_";
    if (!isReset && !isIncrease && !isDecrease) return;
    event.preventDefault();
    const next = readDisplayPreferences();
    if (isReset) {
      next.zoom = defaultUiZoom();
    } else {
      next.zoom = normalizedUiZoom(next.zoom + (isIncrease ? UI_ZOOM_STEP : -UI_ZOOM_STEP));
    }
    writeDisplayPreferences(next);
    applyUiZoom(next.zoom);
    syncDisplaySettingControls(next);
  });
}

export class AutosaveCoordinator {
  constructor({ resourceKey, saveUrl, readValue, readFingerprint, buildBody, statusElement, onSaved, delay = 650 }) {
    this.resourceKey = resourceKey;
    this.saveUrl = saveUrl;
    this.readValue = readValue;
    this.readFingerprint = readFingerprint || readValue;
    this.buildBody = buildBody;
    this.statusElement = statusElement;
    this.onSaved = onSaved || (() => {});
    this.delay = delay;
    this.timer = 0;
    this.inFlight = false;
    this.saveAgain = false;
    this.lastSavedValue = readValue();
    this.lastSavedFingerprint = this.readFingerprint();
    this.revisionKey = `autosave:revision:${resourceKey}`;
    this.revision = Number(viewState.sessionGet(this.revisionKey) || 0);
    this.dirtyRevision = this.revision;
    this.controller = null;
    let sessionId = viewState.sessionGet("autosave:session");
    if (!sessionId) {
      sessionId = globalThis.crypto?.randomUUID
        ? globalThis.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      viewState.sessionSet("autosave:session", sessionId);
    }
    this.sessionId = sessionId;
  }

  setState(state, message) {
    if (!this.statusElement) return;
    this.statusElement.dataset.saveState = state;
    this.statusElement.textContent = message;
  }

  nextRevision() {
    this.revision += 1;
    this.dirtyRevision = this.revision;
    viewState.sessionSet(this.revisionKey, this.revision);
    return this.revision;
  }

  payload(value, revision) {
    const body = this.buildBody(value);
    body.set("autosave_session", this.sessionId);
    body.set("autosave_revision", String(revision));
    return body;
  }

  markDirty() {
    this.nextRevision();
    this.setState("dirty", "待保存");
    window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => this.flush(), this.delay);
  }

  async flush() {
    window.clearTimeout(this.timer);
    const value = this.readValue();
    const fingerprint = this.readFingerprint();
    if (fingerprint === this.lastSavedFingerprint && !this.saveAgain) return;
    if (this.inFlight) {
      this.saveAgain = true;
      return;
    }
    const revision = this.dirtyRevision || this.nextRevision();
    this.inFlight = true;
    this.saveAgain = false;
    this.controller = new AbortController();
    this.setState("saving", "保存中…");
    try {
      const response = await fetch(this.saveUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "Accept": "application/json",
          "X-Gongkao-Autosave": "1",
        },
        body: this.payload(value, revision).toString(),
        signal: this.controller.signal,
      });
      if (!response.ok) throw new Error("save failed");
      const result = await response.json().catch(() => ({ ok: true, accepted: true }));
      if (result.accepted === false) {
        this.setState("dirty", "检测到较新的保存，继续输入后重试");
      } else if (revision === this.dirtyRevision && fingerprint === this.readFingerprint()) {
        this.lastSavedValue = value;
        this.lastSavedFingerprint = fingerprint;
        this.setState("saved", `已保存 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
        this.onSaved(value);
      } else {
        this.setState("dirty", "待保存");
        this.saveAgain = true;
      }
    } catch (error) {
      if (error.name !== "AbortError") this.setState("error", "保存失败，继续输入会重试");
    } finally {
      this.inFlight = false;
      this.controller = null;
      if (this.saveAgain || this.readFingerprint() !== this.lastSavedFingerprint) {
        this.saveAgain = false;
        this.timer = window.setTimeout(() => this.flush(), 80);
      }
    }
  }

  flushOnPageHide() {
    window.clearTimeout(this.timer);
    const value = this.readValue();
    if (this.readFingerprint() === this.lastSavedFingerprint) return;
    const revision = this.dirtyRevision || this.nextRevision();
    const body = this.payload(value, revision);
    if (this.controller) this.controller.abort();
    if (navigator.sendBeacon) {
      navigator.sendBeacon(this.saveUrl, new Blob([body.toString()], {
        type: "application/x-www-form-urlencoded;charset=UTF-8",
      }));
    }
  }
}

export function initializeWorkflowMenus(signal) {
  const records = Array.from(document.querySelectorAll("[data-workflow-menu]"))
    .map((menu) => ({
      menu,
      toggle: menu.querySelector("[data-workflow-menu-toggle]"),
      popover: menu.querySelector("[data-workflow-menu-popover]"),
    }))
    .filter(({ toggle, popover }) => toggle && popover);
  if (!records.length) return;

  const eventOptions = signal ? { signal } : undefined;
  const zoomScale = () => {
    const value = Number.parseFloat(document.documentElement.style.zoom || "1");
    return Number.isFinite(value) && value > 0 ? value : 1;
  };
  const positionPopover = ({ toggle, popover }) => {
    if (popover.hidden) return;
    const scale = zoomScale();
    const anchor = toggle.getBoundingClientRect();
    const popoverRect = popover.getBoundingClientRect();
    const viewportWidth = window.innerWidth * scale;
    const viewportHeight = window.innerHeight * scale;
    const gap = 8 * scale;
    const edge = 8 * scale;
    const left = Math.min(
      Math.max(edge, anchor.right - popoverRect.width),
      Math.max(edge, viewportWidth - popoverRect.width - edge),
    );
    const below = anchor.bottom + gap;
    const above = anchor.top - popoverRect.height - gap;
    const top = below + popoverRect.height <= viewportHeight - edge
      ? below
      : Math.max(edge, above);
    popover.style.left = `${left / scale}px`;
    popover.style.top = `${top / scale}px`;
  };

  const setOpen = (record, open) => {
    const { menu, toggle, popover } = record;
    menu.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      // The top-level portal avoids hit-test offsets from the workflow header,
      // overflow containers, and native WebView zoom. The visual menu stays
      // compact; every visible row is the actual link or button.
      document.body.append(popover);
      popover.hidden = false;
      positionPopover(record);
      return;
    }
    popover.hidden = true;
    popover.style.removeProperty("left");
    popover.style.removeProperty("top");
    menu.append(popover);
  };
  const closeMenus = (except = null) => {
    records.forEach((record) => {
      if (record !== except) setOpen(record, false);
    });
  };

  records.forEach((record) => {
    const { toggle, popover } = record;
    toggle.setAttribute("aria-haspopup", "menu");
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const nextOpen = popover.hidden;
      closeMenus(record);
      setOpen(record, nextOpen);
    }, eventOptions);
    popover.addEventListener("click", (event) => {
      if (event.target.closest("a, button")) setOpen(record, false);
    }, eventOptions);
  });

  document.addEventListener("click", (event) => {
    const insideMenu = records.some(({ menu, popover }) => (
      menu.contains(event.target) || popover.contains(event.target)
    ));
    if (!insideMenu) closeMenus();
  }, eventOptions);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const openRecord = records.find(({ popover }) => !popover.hidden);
    if (!openRecord) return;
    setOpen(openRecord, false);
    openRecord.toggle.focus();
  }, eventOptions);
  const repositionOpenMenus = () => {
    records.forEach((record) => positionPopover(record));
  };
  window.addEventListener("resize", repositionOpenMenus, eventOptions);
  window.addEventListener("scroll", repositionOpenMenus, signal
    ? { capture: true, passive: true, signal }
    : { capture: true, passive: true });
  signal?.addEventListener("abort", () => closeMenus(), { once: true });
}
