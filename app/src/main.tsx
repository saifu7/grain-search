import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import Settings from "./Settings";
import Setup from "./Setup";
import "./styles.css";

declare global {
  interface Window {
    /** Set by the app shell's startup script in the Settings and setup windows. */
    __GRAIN_VIEW__?: string;
  }
}

// One page, three windows: the search bar, Settings, and the first-run setup window. The app
// shell marks the others with window.__GRAIN_VIEW__; in a browser (development) the #hash does it.
function Root() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const on = () => setHash(location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  if (window.__GRAIN_VIEW__ === "setup" || hash === "#setup") return <Setup />;
  return window.__GRAIN_VIEW__ === "settings" || hash === "#settings" ? <Settings /> : <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
