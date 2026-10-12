import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import Settings from "./Settings";
import "./styles.css";

declare global {
  interface Window {
    /** Set by the app shell's startup script in the Settings window. */
    __GRAIN_VIEW__?: string;
  }
}

// One page, two windows: the search bar, and the Settings window. The app shell marks the
// Settings window with window.__GRAIN_VIEW__; in a browser (development) #settings does it.
function Root() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const on = () => setHash(location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return window.__GRAIN_VIEW__ === "settings" || hash === "#settings" ? <Settings /> : <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
