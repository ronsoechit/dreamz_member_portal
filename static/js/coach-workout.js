(() => {
  const configNode = document.getElementById("active-workout-config");
  const shell = document.querySelector("[data-active-workout]");
  if (!configNode || !shell) return;

  let config;
  try {
    config = JSON.parse(configNode.textContent || "{}");
  } catch (_error) {
    return;
  }

  const texts = config.texts || {};
  const exercises = Array.from(shell.querySelectorAll("[data-workout-exercise]"));
  const saveState = shell.querySelector("[data-workout-save-state]");
  const review = shell.querySelector("[data-workout-review]");
  const finishError = shell.querySelector("[data-finish-error]");
  const previousButton = shell.querySelector("[data-previous-exercise]");
  const nextButton = shell.querySelector("[data-next-exercise]");
  const finishButton = shell.querySelector("[data-finish-workout]");
  const discardButton = shell.querySelector("[data-discard-workout]");
  const exitLink = shell.querySelector("[data-workout-exit]");
  const safetyBlocked = shell.dataset.safetyBlocked === "true";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let revision = Number(config.revision || 0);
  let activeOrder = Number(config.activeExerciseOrder || 1);
  let saveChain = Promise.resolve(true);
  let saveInFlight = false;
  let changeVersion = 0;
  let savedChangeVersion = 0;
  let retrySaveEntry = null;
  let inFlightSaveEntry = null;
  let localDraftPresent = false;
  let hasConflict = false;
  let autosaveTimer = null;
  let elapsedSyncTimer = null;
  const elapsedAtPageLoad = Math.max(0, Number(config.elapsedSeconds || 0));
  const elapsedClockStartedAt = Date.now();
  let lastPersistedElapsedSeconds = elapsedAtPageLoad;

  const makeRequestId = () => {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `workout_${Date.now()}_${Math.random().toString(36).slice(2, 12)}`;
  };

  const currentElapsedSeconds = () => Math.min(
    21600,
    Math.max(0, Math.floor(elapsedAtPageLoad + ((Date.now() - elapsedClockStartedAt) / 1000)))
  );

  const setSaveState = (message, state = "") => {
    if (!saveState) return;
    saveState.textContent = message || "";
    saveState.classList.toggle("is-saving", state === "saving");
    saveState.classList.toggle("is-saved", state === "saved");
    saveState.classList.toggle("is-error", state === "error");
  };

  const setInlineError = (exercise, message = "") => {
    const error = exercise?.querySelector("[data-exercise-error]");
    if (!error) return;
    error.textContent = message;
    error.classList.toggle("hidden", !message);
  };

  const numericValue = (input) => {
    const value = input?.value?.trim();
    if (!value) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };

  const exerciseForOrder = (order) => exercises.find(
    (exercise) => Number(exercise.dataset.exerciseOrder) === Number(order)
  );

  const exerciseStatus = (exercise) => exercise?.dataset.exerciseStatus || "pending";

  const collectExercisePayload = (
    exercise,
    nextStatus = null,
    nextOrder = activeOrder,
    requestId = makeRequestId()
  ) => {
    const trackingMode = exercise.dataset.trackingMode || "reps";
    const sets = Array.from(exercise.querySelectorAll("[data-workout-set]")).map((setRow) => {
      const durationMinutes = numericValue(setRow.querySelector("[data-set-duration]"));
      return {
        setNumber: Number(setRow.dataset.setNumber || 0),
        reps: numericValue(setRow.querySelector("[data-set-reps]")),
        durationSeconds: durationMinutes === null ? null : Math.round(durationMinutes * 60),
        weightKg: numericValue(setRow.querySelector("[data-set-weight]")),
        rpe: numericValue(setRow.querySelector("[data-set-rpe]")),
        completed: Boolean(setRow.querySelector("[data-set-completed]")?.checked),
        trackingMode,
      };
    });
    return {
      requestId,
      revision,
      elapsedSeconds: currentElapsedSeconds(),
      exerciseOrder: Number(exercise.dataset.exerciseOrder || 0),
      activeExerciseOrder: Number(nextOrder || exercise.dataset.exerciseOrder || 1),
      exerciseStatus: nextStatus || exerciseStatus(exercise),
      skipReason: exercise.querySelector("[data-skip-reason]")?.value || "",
      sets,
    };
  };

  const draftStorageKey = `dreamz-active-workout-draft:${config.autosaveUrl || window.location.pathname}`;
  const comparableExerciseState = (payload) => ({
    exerciseOrder: Number(payload?.exerciseOrder || 0),
    activeExerciseOrder: Number(payload?.activeExerciseOrder || 0),
    exerciseStatus: payload?.exerciseStatus || "pending",
    skipReason: payload?.skipReason || "",
    sets: Array.isArray(payload?.sets) ? payload.sets.map((setRow) => ({
      setNumber: Number(setRow.setNumber || 0),
      reps: setRow.reps ?? null,
      durationSeconds: setRow.durationSeconds ?? null,
      weightKg: setRow.weightKg ?? null,
      rpe: setRow.rpe ?? null,
      completed: Boolean(setRow.completed),
      trackingMode: setRow.trackingMode || "reps",
    })) : [],
  });

  const readStoredDraft = () => {
    try {
      const draft = JSON.parse(window.sessionStorage.getItem(draftStorageKey) || "null");
      if (!draft?.payload || Date.now() - Number(draft.recordedAt || 0) > 7 * 24 * 60 * 60 * 1000) {
        window.sessionStorage.removeItem(draftStorageKey);
        localDraftPresent = false;
        return null;
      }
      localDraftPresent = true;
      return draft;
    } catch (_error) {
      return null;
    }
  };

  const persistDraftPayload = (payload, version = changeVersion) => {
    try {
      const existing = readStoredDraft();
      if (existing && Number(existing.changeVersion || 0) > Number(version || 0)) return;
      window.sessionStorage.setItem(draftStorageKey, JSON.stringify({
        changeVersion: Number(version || 0),
        recordedAt: Date.now(),
        payload,
      }));
      localDraftPresent = true;
    } catch (_error) {
      // Autosave remains the primary persistence layer when web storage is unavailable.
    }
  };

  const clearStoredDraft = () => {
    try {
      window.sessionStorage.removeItem(draftStorageKey);
    } catch (_error) {
      // Nothing else is required when storage is unavailable.
    }
    localDraftPresent = false;
  };

  const restoreStoredDraft = () => {
    const draft = readStoredDraft();
    if (!draft) return null;
    const payload = draft.payload;
    const exercise = exerciseForOrder(payload.exerciseOrder);
    if (!exercise) {
      clearStoredDraft();
      return null;
    }
    const currentPayload = collectExercisePayload(
      exercise,
      exerciseStatus(exercise),
      activeOrder,
      "draft-comparison"
    );
    if (
      JSON.stringify(comparableExerciseState(currentPayload))
      === JSON.stringify(comparableExerciseState(payload))
    ) {
      clearStoredDraft();
      return null;
    }
    if (Number(payload.revision) !== revision) {
      clearStoredDraft();
      hasConflict = true;
      setSaveState(texts.conflict, "error");
      return null;
    }

    const setRows = new Map(
      Array.from(exercise.querySelectorAll("[data-workout-set]")).map(
        (setRow) => [Number(setRow.dataset.setNumber || 0), setRow]
      )
    );
    (payload.sets || []).forEach((savedSet) => {
      const setRow = setRows.get(Number(savedSet.setNumber || 0));
      if (!setRow) return;
      const durationMinutes = savedSet.durationSeconds === null || savedSet.durationSeconds === undefined
        ? ""
        : String(Number(savedSet.durationSeconds) / 60);
      const values = [
        ["[data-set-reps]", savedSet.reps],
        ["[data-set-duration]", durationMinutes],
        ["[data-set-weight]", savedSet.weightKg],
        ["[data-set-rpe]", savedSet.rpe],
      ];
      values.forEach(([selector, value]) => {
        const input = setRow.querySelector(selector);
        if (input) input.value = value === null || value === undefined ? "" : String(value);
      });
      const completedInput = setRow.querySelector("[data-set-completed]");
      if (completedInput) completedInput.checked = Boolean(savedSet.completed);
    });
    const skipReason = exercise.querySelector("[data-skip-reason]");
    if (skipReason) skipReason.value = payload.skipReason || "";
    exercise.dataset.exerciseStatus = payload.exerciseStatus || "pending";
    activeOrder = Number(payload.activeExerciseOrder || payload.exerciseOrder || activeOrder);
    changeVersion = Math.max(1, Number(draft.changeVersion || 0));
    savedChangeVersion = 0;
    return exercise;
  };

  const requestJson = async (url, payload, { keepalive = false } = {}) => {
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": config.csrfToken || "",
        },
        body: JSON.stringify(payload),
        credentials: "same-origin",
        cache: "no-store",
        keepalive,
      });
    } catch (error) {
      error.retryable = true;
      throw error;
    }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(result.message || texts.saveError || "");
      error.code = result.code || "request_failed";
      error.serverRevision = result.serverRevision;
      error.retryable = response.status === 429 || response.status >= 500;
      throw error;
    }
    return result;
  };

  const applySaveError = (exercise, error) => {
    const message = error?.message || texts.saveError;
    setInlineError(exercise, message);
    if (error?.code === "revision_conflict" || error?.code === "idempotency_conflict") {
      hasConflict = true;
      setSaveState(texts.conflict || message, "error");
      return;
    }
    if (!navigator.onLine) {
      setSaveState(texts.offline || message, "error");
      return;
    }
    setSaveState(message, "error");
  };

  const hasUnsavedChanges = () => (
    changeVersion > savedChangeVersion || Boolean(retrySaveEntry) || localDraftPresent
  );

  const sendSaveEntry = async (entry, options = {}) => {
    saveInFlight = true;
    inFlightSaveEntry = entry;
    setSaveState(texts.saving, "saving");
    setInlineError(entry.exercise);
    try {
      const result = await requestJson(config.autosaveUrl, entry.payload, options);
      revision = Number(result.revision ?? revision);
      config.revision = revision;
      lastPersistedElapsedSeconds = Math.max(
        lastPersistedElapsedSeconds,
        Number(entry.payload.elapsedSeconds || 0)
      );
      if (retrySaveEntry?.payload.requestId === entry.payload.requestId) {
        retrySaveEntry = null;
      }
      savedChangeVersion = Math.max(savedChangeVersion, entry.changeVersion);
      const hasNewerChanges = changeVersion > entry.changeVersion;
      if (entry.nextStatus) {
        entry.exercise.dataset.exerciseStatus = hasNewerChanges ? "pending" : entry.nextStatus;
      }
      activeOrder = hasNewerChanges
        ? Number(entry.payload.exerciseOrder || activeOrder)
        : Number(entry.nextOrder || activeOrder);
      if (changeVersion <= savedChangeVersion && !retrySaveEntry) clearStoredDraft();
      const clean = !hasUnsavedChanges();
      setSaveState(
        clean ? (result.message || texts.saved) : texts.saving,
        clean ? "saved" : "saving"
      );
      updateInterface();
      return clean;
    } catch (error) {
      if (error?.retryable) retrySaveEntry = entry;
      applySaveError(entry.exercise, error);
      return false;
    } finally {
      if (inFlightSaveEntry?.payload.requestId === entry.payload.requestId) {
        inFlightSaveEntry = null;
      }
      saveInFlight = false;
    }
  };

  const saveExercise = (exercise, nextStatus = null, nextOrder = activeOrder, options = {}) => {
    if (!exercise || safetyBlocked || config.status !== "active" || hasConflict) {
      return Promise.resolve(false);
    }
    window.clearTimeout(autosaveTimer);
    const runSave = async () => {
      if (retrySaveEntry) {
        const recovered = await sendSaveEntry(retrySaveEntry, options);
        if (!recovered && retrySaveEntry) return false;
        if (!hasUnsavedChanges()) return true;
      }

      const entry = {
        exercise,
        nextStatus,
        nextOrder,
        changeVersion,
        payload: collectExercisePayload(exercise, nextStatus, nextOrder),
      };
      persistDraftPayload(entry.payload, entry.changeVersion);
      return sendSaveEntry(entry, options);
    };
    saveChain = saveChain.then(runSave, runSave);
    return saveChain;
  };

  const scheduleAutosave = (exercise) => {
    if (!exercise || safetyBlocked || hasConflict) return;
    window.clearTimeout(autosaveTimer);
    autosaveTimer = window.setTimeout(() => {
      saveExercise(exercise, exerciseStatus(exercise), activeOrder);
    }, 650);
  };

  const resolvedExerciseCount = () => exercises.filter(
    (exercise) => ["completed", "skipped"].includes(exerciseStatus(exercise))
  ).length;

  const completedSetCount = () => shell.querySelectorAll(
    "[data-set-completed]:checked"
  ).length;

  const updateSetRows = () => {
    shell.querySelectorAll("[data-workout-set]").forEach((setRow) => {
      const completed = Boolean(setRow.querySelector("[data-set-completed]")?.checked);
      setRow.classList.toggle("is-completed", completed);
    });
  };

  const updateOverview = () => {
    shell.querySelectorAll("[data-jump-exercise]").forEach((button) => {
      const exercise = exerciseForOrder(button.dataset.jumpExercise);
      const status = exerciseStatus(exercise);
      const icon = button.querySelector("[data-overview-icon]");
      const statusLabel = button.querySelector("[data-overview-status-label]");
      const isActive = Number(button.dataset.jumpExercise) === activeOrder;
      button.dataset.overviewStatus = status;
      button.classList.toggle("is-active", isActive);
      if (isActive) button.setAttribute("aria-current", "step");
      else button.removeAttribute("aria-current");
      if (icon) icon.textContent = status === "completed" ? "✓" : status === "skipped" ? "—" : "○";
      if (statusLabel) statusLabel.textContent = texts[status] || "";
      const completedSets = exercise?.querySelectorAll("[data-set-completed]:checked").length || 0;
      const totalSets = exercise?.querySelectorAll("[data-workout-set]").length || 0;
      const small = button.querySelector("small");
      if (small) small.textContent = `${completedSets}/${totalSets} ${String(texts.setsLabel || "").toLowerCase()}`;
    });
  };

  const updateStatusLabels = () => {
    exercises.forEach((exercise) => {
      const status = exerciseStatus(exercise);
      const label = exercise.querySelector("[data-exercise-status-label]");
      if (!label) return;
      label.textContent = texts[status] || "";
      label.dataset.status = status;
    });
  };

  const updateProgress = () => {
    const resolved = resolvedExerciseCount();
    const percentage = exercises.length ? Math.round((resolved / exercises.length) * 100) : 0;
    const currentIndex = Math.max(0, exercises.findIndex(
      (exercise) => Number(exercise.dataset.exerciseOrder) === activeOrder
    ));
    const progressLabel = shell.querySelector("[data-workout-progress-label]");
    const progressPercent = shell.querySelector("[data-workout-progress-percent]");
    const progressBar = shell.querySelector("[data-workout-progress-bar]");
    if (progressLabel) {
      progressLabel.textContent = String(texts.exerciseProgress || "")
        .replace("{current}", String(currentIndex + 1))
        .replace("{total}", String(exercises.length));
    }
    if (progressPercent) {
      progressPercent.textContent = String(texts.percentComplete || "{percent}%")
        .replace("{percent}", String(percentage));
    }
    if (progressBar) progressBar.style.width = `${percentage}%`;
  };

  const updateReview = () => {
    const resolved = resolvedExerciseCount();
    const completed = exercises.filter(
      (exercise) => exerciseStatus(exercise) === "completed"
    ).length;
    const skipped = exercises.filter(
      (exercise) => exerciseStatus(exercise) === "skipped"
    ).length;
    const ready = exercises.length > 0 && resolved === exercises.length && completed > 0;
    review?.classList.toggle("hidden", !ready);
    const reviewExercises = shell.querySelector("[data-review-exercises]");
    const reviewSets = shell.querySelector("[data-review-sets]");
    const reviewSkipped = shell.querySelector("[data-review-skipped]");
    if (reviewExercises) reviewExercises.textContent = String(completed);
    if (reviewSets) reviewSets.textContent = String(completedSetCount());
    if (reviewSkipped) reviewSkipped.textContent = String(skipped);
    if (finishButton) finishButton.disabled = safetyBlocked || !ready || hasConflict;
  };

  const showExercise = (order, { scroll = true, focus = false } = {}) => {
    const target = exerciseForOrder(order);
    if (!target) return;
    activeOrder = Number(order);
    exercises.forEach((exercise) => {
      exercise.classList.toggle(
        "hidden",
        Number(exercise.dataset.exerciseOrder) !== activeOrder
      );
    });
    if (previousButton) previousButton.disabled = activeOrder <= 1;
    if (nextButton) {
      nextButton.disabled = activeOrder >= exercises.length;
      nextButton.classList.toggle("hidden", activeOrder >= exercises.length);
    }
    updateInterface();
    if (scroll) {
      target.scrollIntoView({
        behavior: reducedMotion.matches ? "auto" : "smooth",
        block: "start",
      });
    }
    if (focus) {
      target.querySelector("[data-exercise-heading]")?.focus({ preventScroll: true });
    }
  };

  const showReview = ({ focus = false } = {}) => {
    review?.scrollIntoView({
      behavior: reducedMotion.matches ? "auto" : "smooth",
      block: "start",
    });
    if (focus) {
      review?.querySelector("[data-workout-review-heading]")?.focus({ preventScroll: true });
    }
  };

  const updateInterface = () => {
    updateSetRows();
    updateStatusLabels();
    updateOverview();
    updateProgress();
    updateReview();
  };

  exercises.forEach((exercise) => {
    exercise.querySelectorAll("input, select").forEach((input) => {
      const eventName = input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input";
      input.addEventListener(eventName, () => {
        changeVersion += 1;
        if (input.matches("[data-set-completed]")) updateSetRows();
        if (exerciseStatus(exercise) !== "pending") {
          exercise.dataset.exerciseStatus = "pending";
        }
        persistDraftPayload(
          collectExercisePayload(exercise, exerciseStatus(exercise), activeOrder),
          changeVersion
        );
        scheduleAutosave(exercise);
        updateInterface();
      });
    });

    exercise.querySelector("[data-complete-exercise]")?.addEventListener("click", async () => {
      const order = Number(exercise.dataset.exerciseOrder || 1);
      const nextOrder = Math.min(exercises.length, order + 1);
      const saved = await saveExercise(exercise, "completed", nextOrder);
      if (saved && order < exercises.length) showExercise(nextOrder, { focus: true });
      if (saved && order >= exercises.length) showReview({ focus: true });
    });

    exercise.querySelector("[data-skip-exercise]")?.addEventListener("click", async () => {
      const reason = exercise.querySelector("[data-skip-reason]")?.value || "";
      if (!reason) {
        setInlineError(exercise, texts.skipReason);
        return;
      }
      const order = Number(exercise.dataset.exerciseOrder || 1);
      const nextOrder = Math.min(exercises.length, order + 1);
      const saved = await saveExercise(exercise, "skipped", nextOrder);
      if (saved && order < exercises.length) showExercise(nextOrder, { focus: true });
      if (saved && order >= exercises.length) showReview({ focus: true });
    });
  });

  previousButton?.addEventListener("click", async () => {
    const targetOrder = Math.max(1, activeOrder - 1);
    if (safetyBlocked || hasConflict) {
      showExercise(targetOrder, { focus: true });
      return;
    }
    const exercise = exerciseForOrder(activeOrder);
    const saved = await saveExercise(exercise, exerciseStatus(exercise), targetOrder);
    if (saved) showExercise(targetOrder, { focus: true });
  });

  nextButton?.addEventListener("click", async () => {
    const targetOrder = Math.min(exercises.length, activeOrder + 1);
    if (safetyBlocked || hasConflict) {
      showExercise(targetOrder, { focus: true });
      return;
    }
    const exercise = exerciseForOrder(activeOrder);
    const saved = await saveExercise(exercise, exerciseStatus(exercise), targetOrder);
    if (saved) showExercise(targetOrder, { focus: true });
  });

  shell.querySelectorAll("[data-jump-exercise]").forEach((button) => {
    button.addEventListener("click", async () => {
      const targetOrder = Number(button.dataset.jumpExercise || activeOrder);
      if (safetyBlocked || hasConflict) {
        showExercise(targetOrder, { focus: true });
        return;
      }
      const exercise = exerciseForOrder(activeOrder);
      const saved = await saveExercise(exercise, exerciseStatus(exercise), targetOrder);
      if (saved) showExercise(targetOrder, { focus: true });
    });
  });

  finishButton?.addEventListener("click", async () => {
    if (hasConflict || safetyBlocked) return;
    await saveChain;
    if (hasUnsavedChanges() || saveInFlight) {
      if (finishError) {
        finishError.textContent = texts.saveError;
        finishError.classList.remove("hidden");
      }
      return;
    }
    if (!window.confirm(texts.finishConfirm || "")) return;
    finishButton.disabled = true;
    finishButton.setAttribute("aria-busy", "true");
    finishButton.textContent = texts.finishing;
    if (finishError) finishError.classList.add("hidden");
    try {
      const result = await requestJson(config.finishUrl, {
        requestId: makeRequestId(),
        revision,
        elapsedSeconds: currentElapsedSeconds(),
      });
      config.status = "completed";
      window.clearInterval(elapsedSyncTimer);
      const canShowAchievements = typeof window.showDreamzAchievement === "function";
      if (Array.isArray(result.milestones)) {
        result.milestones.forEach((milestone, index) => {
          window.setTimeout(() => window.showDreamzAchievement?.(milestone), index * 420);
        });
      }
      const milestoneCount = Array.isArray(result.milestones) ? result.milestones.length : 0;
      window.setTimeout(
        () => window.showDreamzAchievement?.({ type: "workout_completed" }),
        milestoneCount * 420
      );
      clearStoredDraft();
      const navigationDelay = canShowAchievements
        ? Math.min(2600, 1050 + (milestoneCount * 420))
        : 0;
      window.setTimeout(
        () => window.location.assign(result.url || window.location.href),
        navigationDelay
      );
    } catch (error) {
      finishButton.disabled = false;
      finishButton.removeAttribute("aria-busy");
      finishButton.textContent = texts.finish;
      if (finishError) {
        finishError.textContent = error.message || texts.finishBlocked;
        finishError.classList.remove("hidden");
      }
      if (error.serverRevision !== undefined) {
        hasConflict = true;
        setSaveState(texts.conflict, "error");
      }
    }
  });

  discardButton?.addEventListener("click", async () => {
    if (!window.confirm(texts.discardConfirm || "")) return;
    window.clearTimeout(autosaveTimer);
    await saveChain;
    discardButton.disabled = true;
    discardButton.setAttribute("aria-busy", "true");
    discardButton.textContent = texts.discarding;
    try {
      const result = await requestJson(config.discardUrl, { revision });
      config.status = "abandoned";
      window.clearInterval(elapsedSyncTimer);
      clearStoredDraft();
      window.location.assign(result.url || config.coachUrl);
    } catch (error) {
      discardButton.disabled = false;
      discardButton.removeAttribute("aria-busy");
      discardButton.textContent = texts.discard;
      setSaveState(error.message || texts.saveError, "error");
    }
  });

  window.addEventListener("offline", () => {
    setSaveState(texts.offline, "error");
  });
  window.addEventListener("online", () => {
    if (hasUnsavedChanges() && !saveInFlight) {
      const exercise = exerciseForOrder(activeOrder);
      saveExercise(exercise, exerciseStatus(exercise), activeOrder).then((saved) => {
        if (saved) showExercise(activeOrder, { scroll: false });
      });
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (!hasUnsavedChanges() && !saveInFlight) return;
    event.preventDefault();
    event.returnValue = "";
  });
  const persistElapsedTime = ({ keepalive = false } = {}) => {
    const elapsedSeconds = currentElapsedSeconds();
    if (
      !config.elapsedUrl
      || config.status !== "active"
      || safetyBlocked
      || elapsedSeconds <= lastPersistedElapsedSeconds
    ) {
      return Promise.resolve(false);
    }
    return requestJson(
      config.elapsedUrl,
      { elapsedSeconds },
      { keepalive }
    ).then((result) => {
      lastPersistedElapsedSeconds = Math.max(
        lastPersistedElapsedSeconds,
        Number(result.elapsedSeconds || elapsedSeconds)
      );
      return true;
    }).catch(() => false);
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      persistElapsedTime({ keepalive: true });
    }
  });
  if (config.status === "active" && !safetyBlocked) {
    elapsedSyncTimer = window.setInterval(() => persistElapsedTime(), 30000);
  }
  exitLink?.addEventListener("click", async (event) => {
    if (
      event.defaultPrevented
      || event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) {
      return;
    }
    event.preventDefault();
    await persistElapsedTime();
    window.location.assign(exitLink.href);
  });
  window.addEventListener("pagehide", () => {
    persistElapsedTime({ keepalive: true });
    if (hasConflict || safetyBlocked || config.status !== "active") return;
    if (!saveInFlight && !hasUnsavedChanges()) return;
    window.clearTimeout(autosaveTimer);
    const exercise = exerciseForOrder(activeOrder);
    if (!exercise) return;
    const payload = inFlightSaveEntry?.payload
      || retrySaveEntry?.payload
      || collectExercisePayload(exercise, exerciseStatus(exercise), activeOrder);
    requestJson(config.autosaveUrl, payload, { keepalive: true }).catch(() => {});
  });

  const timer = shell.querySelector("[data-workout-timer]");
  if (timer) {
    const renderTimer = () => {
      const elapsedSeconds = currentElapsedSeconds();
      const hours = Math.floor(elapsedSeconds / 3600);
      const minutes = Math.floor((elapsedSeconds % 3600) / 60);
      const seconds = Math.floor(elapsedSeconds % 60);
      timer.textContent = hours > 0
        ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
        : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    };
    renderTimer();
    if (config.status === "active" && !safetyBlocked) window.setInterval(renderTimer, 1000);
  }

  if (exercises.length) showExercise(activeOrder, { scroll: false });
  updateInterface();
  const restoredDraftExercise = restoreStoredDraft();
  if (restoredDraftExercise) {
    showExercise(activeOrder, { scroll: false });
    updateInterface();
    setSaveState(texts.saving, "saving");
    scheduleAutosave(restoredDraftExercise);
  } else if (hasConflict) {
    updateInterface();
  }
})();
