import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

const BOOT_SPLASH_ID = "boot-splash";
const BOOT_SPLASH_LEAVE_CLASS = "boot-splash-leave";
const BOOT_SPLASH_ANIMATION_MS = 240;

function hideBootSplash() {
  const splash = document.getElementById(BOOT_SPLASH_ID);
  if (!splash) return;
  splash.classList.add(BOOT_SPLASH_LEAVE_CLASS);
  window.setTimeout(() => splash.remove(), BOOT_SPLASH_ANIMATION_MS);
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App onAppReady={hideBootSplash} />
  </React.StrictMode>,
);
