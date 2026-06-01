(() => {
  const defaults = window.DREAMZ_ACHIEVEMENT_TEXTS || {};
  const majorTypes = new Set(["personal_record", "streak_reached", "goal_reached", "challenge_completed"]);
  const queue = [];
  const activeToasts = new Set();
  const recentKeys = new Map();
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const iconPaths = {
    dumbbell: '<path d="M4 9h3v6H4zM17 9h3v6h-3zM7 11h10v2H7zM2.8 10h1.2v4H2.8zM20 10h1.2v4H20z"/>',
    meal: '<path d="M7 3v8M5 3v4M9 3v4M5 7h4M7 11v10M15 3c2.2 1.5 3 3.8 2.1 6.8-.3 1-.9 1.9-1.8 2.6V21h-2V3z"/>',
    camera: '<path d="M4 7h3l1.3-2h7.4L17 7h3v12H4z"/><circle cx="12" cy="13" r="3.2"/>',
    progress: '<path d="M4 19h16M6 16v-5M12 16V6M18 16v-8"/>',
    calendar: '<path d="M5 5h14v15H5zM8 3v4M16 3v4M5 9h14"/>',
    trophy: '<path d="M8 4h8v4a4 4 0 0 1-8 0zM8 6H5a3 3 0 0 0 3 4M16 6h3a3 3 0 0 1-3 4M12 12v4M9 20h6M10 16h4"/>',
    streak: '<path d="M12 21c3-2 5-4.7 5-7.8 0-2.6-1.6-5-4.6-7.2.1 2-.6 3.4-2.1 4.4.1-1.8-.7-3.7-2.3-5.4C6.3 7.2 5 10 5 13.2 5 16.3 8 19.4 12 21z"/>',
    target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1.4"/>',
  };

  const getLayer = () => {
    let layer = document.querySelector("[data-dreamz-achievement-layer]");
    if (layer) return layer;
    layer = document.createElement("div");
    layer.className = "dreamz-achievement-layer";
    layer.dataset.dreamzAchievementLayer = "true";
    document.body.append(layer);
    return layer;
  };

  const iconMarkup = (icon) => `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      ${iconPaths[icon] || iconPaths.trophy}
    </svg>
  `;

  const normalizeAchievement = (input = {}) => {
    const type = String(input.type || "goal_reached");
    const preset = defaults[type] || {};
    const level = input.level || preset.level || (majorTypes.has(type) ? "major" : "standard");
    return {
      type,
      level,
      icon: input.icon || preset.icon || (level === "major" ? "trophy" : "target"),
      title: input.title || preset.title || "Dreamz achievement",
      message: input.message || preset.message || "",
      duration: Number(input.duration || (level === "major" ? 4200 : 3400)),
    };
  };

  const isDuplicate = (item) => {
    const now = Date.now();
    const key = [item.type, item.title, item.message].join("|");
    const last = recentKeys.get(key) || 0;
    recentKeys.set(key, now);
    return now - last < 1800;
  };

  const spawnFloatingReward = (item) => {
    if (reducedMotion.matches) return;
    const reward = document.createElement("div");
    reward.className = `dreamz-floating-reward ${item.level === "major" ? "dreamz-achievement-major" : ""}`;
    reward.innerHTML = iconMarkup(item.icon);
    document.body.append(reward);
    window.setTimeout(() => reward.remove(), 1400);
  };

  const spawnMajorParticles = (toast) => {
    if (reducedMotion.matches) return;
    const particles = document.createElement("div");
    particles.className = "dreamz-achievement-particles";
    for (let index = 0; index < 10; index += 1) {
      const particle = document.createElement("span");
      particle.style.setProperty("--particle-index", index);
      particles.append(particle);
    }
    toast.append(particles);
  };

  const dismissToast = (toast) => {
    toast.classList.add("is-leaving");
    window.setTimeout(() => {
      activeToasts.delete(toast);
      toast.remove();
      drainQueue();
    }, reducedMotion.matches ? 80 : 360);
  };

  const renderAchievement = (item) => {
    const layer = getLayer();
    const toast = document.createElement("div");
    toast.className = `dreamz-achievement-toast ${item.level === "major" ? "dreamz-achievement-major" : ""}`;
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    toast.dataset.achievementType = item.type;
    toast.innerHTML = `
      <div class="dreamz-achievement-icon">${iconMarkup(item.icon)}</div>
      <div class="min-w-0">
        <div class="dreamz-achievement-title"></div>
        <div class="dreamz-achievement-message"></div>
      </div>
    `;
    toast.querySelector(".dreamz-achievement-title").textContent = item.title;
    toast.querySelector(".dreamz-achievement-message").textContent = item.message;
    if (item.level === "major") spawnMajorParticles(toast);
    layer.append(toast);
    activeToasts.add(toast);
    spawnFloatingReward(item);
    window.requestAnimationFrame(() => toast.classList.add("is-visible"));
    window.setTimeout(() => dismissToast(toast), item.duration);
  };

  function drainQueue() {
    while (queue.length && activeToasts.size < 2) {
      renderAchievement(queue.shift());
    }
  }

  window.showDreamzAchievement = (input) => {
    const item = normalizeAchievement(input);
    if (isDuplicate(item)) return;
    queue.push(item);
    drainQueue();
  };

  // Prepared for future backend-confirmed milestones; do not trigger these until
  // the server reports a real PR, streak, goal or challenge completion event.
  window.showDreamzAchievement.majorTypes = Array.from(majorTypes);
  window.dispatchEvent(new CustomEvent("dreamz:achievements-ready"));
})();
