(function () {
  const promptEl = document.querySelector("[data-pwa-install]");
  if (!promptEl) return;

  const installButton = promptEl.querySelector("[data-pwa-install-button]");
  const closeButton = promptEl.querySelector("[data-pwa-install-close]");
  const iosHint = promptEl.querySelector("[data-pwa-ios-hint]");
  const statusEl = promptEl.querySelector("[data-pwa-status]");
  const storageKey = "dreamz-pwa-install-dismissed";

  const isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  const isIos = /iphone|ipad|ipod/i.test(window.navigator.userAgent || "");
  const isSafari = /^((?!chrome|android).)*safari/i.test(window.navigator.userAgent || "");

  if (isStandalone || window.localStorage.getItem(storageKey) === "1") {
    promptEl.remove();
    return;
  }

  let deferredPrompt = null;

  const showPrompt = () => {
    promptEl.hidden = false;
    promptEl.classList.remove("hidden");
  };

  if (isIos && isSafari && iosHint) {
    iosHint.hidden = false;
    showPrompt();
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    showPrompt();
    if (installButton) installButton.hidden = false;
  });

  installButton?.addEventListener("click", async () => {
    if (!deferredPrompt) {
      if (iosHint) {
        iosHint.hidden = false;
        showPrompt();
      }
      return;
    }

    installButton.disabled = true;
    if (statusEl) statusEl.hidden = false;
    deferredPrompt.prompt();
    await deferredPrompt.userChoice.catch(() => null);
    deferredPrompt = null;
    promptEl.remove();
  });

  closeButton?.addEventListener("click", () => {
    window.localStorage.setItem(storageKey, "1");
    promptEl.remove();
  });
})();
