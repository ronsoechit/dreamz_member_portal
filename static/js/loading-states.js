(() => {
  const texts = window.DREAMZ_LOADING_TEXTS || {};
  const loader = document.querySelector("[data-page-loader]");
  const loaderText = loader?.querySelector("[data-page-loader-text]");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let loaderTimer = 0;
  let loaderVisible = false;

  const spinnerMarkup = '<span class="dreamz-loading-spinner" aria-hidden="true"></span>';
  const defaultText = () => texts.loading_please_wait || "Loading...";

  const routeLabelForUrl = (url) => {
    if (!url) return defaultText();
    const path = url.pathname || "";
    if (path === "/" || path.startsWith("/dashboard")) return texts.loading_dashboard || defaultText();
    if (path.startsWith("/coach")) return texts.loading_my_coach || texts.loading_workout || defaultText();
    if (path.startsWith("/nutrition")) return texts.loading_nutrition || texts.loading_meal_plan || defaultText();
    if (path.startsWith("/group-classes")) return texts.loading_classes || defaultText();
    if (path.startsWith("/equipment")) return texts.loading_equipment || defaultText();
    if (path.startsWith("/progress")) return texts.loading_progress || texts.loading_progress_photos || defaultText();
    if (path.startsWith("/account") || path.startsWith("/membership-options") || path.startsWith("/set-password")) {
      return texts.loading_account || defaultText();
    }
    return defaultText();
  };

  const showPageLoader = (label) => {
    if (!loader) return;
    window.clearTimeout(loaderTimer);
    loaderTimer = window.setTimeout(() => {
      if (loaderText) loaderText.textContent = label || defaultText();
      loader.hidden = false;
      loaderVisible = true;
      window.requestAnimationFrame(() => loader.classList.add("is-visible"));
    }, reducedMotion.matches ? 0 : 300);
  };

  const hidePageLoader = () => {
    window.clearTimeout(loaderTimer);
    if (!loader || !loaderVisible) return;
    loader.classList.remove("is-visible");
    window.setTimeout(() => {
      loader.hidden = true;
      loaderVisible = false;
    }, reducedMotion.matches ? 0 : 180);
  };

  const shouldSkipLink = (link, event) => {
    if (!link || link.dataset.noLoading === "true" || link.target === "_blank" || link.hasAttribute("download")) return true;
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return true;
    const href = link.getAttribute("href") || "";
    if (!href || href.startsWith("javascript:") || href.startsWith("mailto:") || href.startsWith("tel:")) return true;
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) return true;
    return url.pathname === window.location.pathname && url.search === window.location.search && url.hash;
  };

  const setElementBusy = (element, label, replaceContent = true) => {
    if (!element || element.dataset.loadingActive === "true") return false;
    element.dataset.loadingActive = "true";
    element.setAttribute("aria-busy", "true");
    element.classList.add("is-loading");
    if ("disabled" in element) element.disabled = true;
    if (replaceContent && label && element.tagName === "INPUT") {
      if (!element.dataset.originalValue) element.dataset.originalValue = element.value;
      element.value = label;
    } else if (replaceContent && label) {
      if (!element.dataset.originalHtml) element.dataset.originalHtml = element.innerHTML;
      element.innerHTML = `<span class="dreamz-button-loading-content">${spinnerMarkup}<span>${label}</span></span>`;
    }
    return true;
  };

  const formHasFile = (form) => Array.from(form.querySelectorAll('input[type="file"]')).some((input) => input.files?.length);

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }

    form.dataset.submitting = "true";
    const submitter = event.submitter || form.querySelector("button[type='submit'], button:not([type]), input[type='submit']");
    const label = submitter?.dataset.loadingText
      || submitter?.dataset.loadingLabel
      || form.dataset.loadingText
      || (formHasFile(form) ? texts.uploading : texts.saving)
      || texts.processing
      || defaultText();
    setElementBusy(submitter, label, true);
    form.querySelector("[data-loading-message]")?.classList.remove("hidden");

    const action = new URL(form.getAttribute("action") || window.location.href, window.location.href);
    showPageLoader(label || routeLabelForUrl(action));
  }, true);

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (shouldSkipLink(link, event)) return;
    if (link.dataset.loadingActive === "true") {
      event.preventDefault();
      return;
    }
    const url = new URL(link.getAttribute("href"), window.location.href);
    const label = link.dataset.loadingLabel || routeLabelForUrl(url);
    setElementBusy(link, "", false);
    showPageLoader(label);
  }, true);

  window.addEventListener("pageshow", hidePageLoader);
  window.addEventListener("pagehide", () => window.clearTimeout(loaderTimer));
})();
