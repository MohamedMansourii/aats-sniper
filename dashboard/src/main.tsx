import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import "./index.css";
import App from "./App.tsx";

// AATS Sniper runs standalone on mock data (see src/lib/api.ts). No tRPC /
// react-query provider is mounted here: the dashboard must render without the
// old backend. When wired to the control plane, set VITE_USE_MOCK=false.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
