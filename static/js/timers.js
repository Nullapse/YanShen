import { viewState } from "./core.js";

export function practiceTimerStorageKey(key) {
  return `gongkao.practiceTimer:v1:${key || "default"}`;
}

export function readPracticeTimerState(key) {
  try {
    const parsed = JSON.parse(viewState.persistentGet(practiceTimerStorageKey(key)) || "{}");
    return {
      elapsedMs: Math.max(0, Number(parsed.elapsedMs) || 0),
      running: Boolean(parsed.running),
      startedAt: Math.max(0, Number(parsed.startedAt) || 0),
    };
  } catch (error) {
    return { elapsedMs: 0, running: false, startedAt: 0 };
  }
}

export function writePracticeTimerState(key, state) {
  try {
    viewState.persistentSet(practiceTimerStorageKey(key), JSON.stringify(state));
  } catch (error) {
    // Timer persistence is helpful but should never block writing an answer.
  }
}

export function removePracticeTimerState(key) {
  try {
    viewState.persistentRemove(practiceTimerStorageKey(key));
  } catch (error) {
    // Ignore storage failures.
  }
}

export function paperTimeExcludedStorageKey(key) {
  return `gongkao.paperTimeExcluded:v1:${key || "default"}`;
}

export function readPaperTimeExcluded(key) {
  try {
    return viewState.persistentGet(paperTimeExcludedStorageKey(key)) === "1";
  } catch (error) {
    return false;
  }
}

export function writePaperTimeExcluded(key, excluded) {
  try {
    viewState.persistentSet(paperTimeExcludedStorageKey(key), excluded ? "1" : "0");
  } catch (error) {
    // Ignore storage failures.
  }
}

export function removePaperTimeExcluded(key) {
  try {
    viewState.persistentRemove(paperTimeExcludedStorageKey(key));
  } catch (error) {
    // Ignore storage failures.
  }
}

export function practiceTimerElapsedMs(state, now = Date.now()) {
  if (!state.running || !state.startedAt) return Math.max(0, state.elapsedMs || 0);
  return Math.max(0, (state.elapsedMs || 0) + now - state.startedAt);
}

export function formatTimerSeconds(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function bindPracticeTimer(container) {
  const key = container.dataset.timerKey || `${container.dataset.timerKind || "timer"}:${location.pathname}`;
  const shouldAutoStart = container.dataset.timerAutostart === "1";
  const display = container.querySelector("[data-timer-display]");
  const paperDisplay = container.querySelector("[data-paper-derived-display]");
  const excludeInput = container.querySelector("[data-paper-time-excluded]");
  const toggle = container.querySelector("[data-timer-toggle]");
  const reset = container.querySelector("[data-timer-reset]");
  const paperBaseSeconds = Math.max(0, Number(container.dataset.paperBaseSeconds) || 0);
  const questionBaseSeconds = Math.max(0, Number(container.dataset.questionBaseSeconds) || 0);
  let state = readPracticeTimerState(key);
  let cleared = false;
  if (excludeInput) {
    excludeInput.checked = readPaperTimeExcluded(key);
  }
  if (shouldAutoStart) {
    state = { elapsedMs: practiceTimerElapsedMs(state), running: true, startedAt: Date.now() };
    writePracticeTimerState(key, state);
    const url = new URL(window.location.href);
    if (url.searchParams.get("timer") === "auto") {
      url.searchParams.delete("timer");
      window.history.replaceState(window.history.state, "", url);
    }
  } else if (state.running) {
    state = { elapsedMs: practiceTimerElapsedMs(state), running: false, startedAt: 0 };
    writePracticeTimerState(key, state);
  }
  const seconds = () => Math.floor(practiceTimerElapsedMs(state) / 1000);
  const countsTowardPaper = () => !excludeInput?.checked;
  const paperSeconds = () => paperBaseSeconds + (countsTowardPaper() ? Math.max(questionBaseSeconds, seconds()) : questionBaseSeconds);
  const render = () => {
    if (display) display.textContent = formatTimerSeconds(seconds());
    if (paperDisplay) paperDisplay.textContent = formatTimerSeconds(paperSeconds());
    if (toggle) toggle.textContent = state.running ? "\u6682\u505c" : (seconds() > 0 ? "\u7ee7\u7eed" : "\u5f00\u59cb");
    container.classList.toggle("is-paused", !state.running);
  };
  const persistCurrent = (pause = false) => {
    state = {
      elapsedMs: practiceTimerElapsedMs(state),
      running: pause ? false : state.running,
      startedAt: !pause && state.running ? Date.now() : 0,
    };
    writePracticeTimerState(key, state);
  };
  toggle?.addEventListener("click", () => {
    if (state.running) {
      state = { elapsedMs: practiceTimerElapsedMs(state), running: false, startedAt: 0 };
    } else {
      state = { elapsedMs: practiceTimerElapsedMs(state), running: true, startedAt: Date.now() };
    }
    writePracticeTimerState(key, state);
    render();
  });
  reset?.addEventListener("click", () => {
    const now = Date.now();
    const wasRunning = state.running;
    state = { elapsedMs: 0, running: wasRunning, startedAt: wasRunning ? now : 0 };
    writePracticeTimerState(key, state);
    render();
  });
  excludeInput?.addEventListener("change", () => {
    writePaperTimeExcluded(key, excludeInput.checked);
    render();
  });
  const persistTimerBeforeNavigation = () => {
    if (!cleared) persistCurrent(true);
  };
  const pageSignal = window.__gongkaoPageAbortController?.signal;
  window.addEventListener("pagehide", persistTimerBeforeNavigation, pageSignal ? { signal: pageSignal } : undefined);
  window.addEventListener("gongkao:before-navigation", persistTimerBeforeNavigation, pageSignal ? { signal: pageSignal } : undefined);
  window.__gongkaoPageIntervals = window.__gongkaoPageIntervals || [];
  window.__gongkaoPageIntervals.push(window.setInterval(render, 1000));
  render();
  container.__practiceTimer = {
    key,
    seconds,
    paperSeconds,
    persist: persistCurrent,
    clear: () => {
      cleared = true;
      removePracticeTimerState(key);
      removePaperTimeExcluded(key);
    },
  };
}

export function bindPaperSummaryTimer(container) {
  const displays = container.querySelectorAll("[data-timer-display]");
  const questionKey = container.dataset.questionTimerKey || "";
  const questionBaseSeconds = Math.max(0, Number(container.dataset.questionBaseSeconds) || 0);
  const paperBaseSeconds = Math.max(0, Number(container.dataset.paperBaseSeconds) || 0);
  let draftSeconds = 0;
  if (questionKey && questionKey !== "question-none") {
    let state = readPracticeTimerState(questionKey);
    draftSeconds = Math.floor(practiceTimerElapsedMs(state) / 1000);
    state = { elapsedMs: draftSeconds * 1000, running: false, startedAt: 0 };
    writePracticeTimerState(questionKey, state);
  }
  const excluded = questionKey ? readPaperTimeExcluded(questionKey) : false;
  if (displays[0]) displays[0].textContent = formatTimerSeconds(Math.max(questionBaseSeconds, draftSeconds));
  if (displays[1]) displays[1].textContent = formatTimerSeconds(paperBaseSeconds + (excluded ? questionBaseSeconds : Math.max(questionBaseSeconds, draftSeconds)));
  container.classList.add("is-paused");
}
