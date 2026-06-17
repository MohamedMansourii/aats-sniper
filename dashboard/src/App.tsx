import { lazy, Suspense } from "react";
import { Routes, Route, Outlet } from "react-router";
import Layout from "./components/Layout";

// Routes are code-split: each page (and the heavy recharts it pulls in) loads
// on demand, keeping the initial bundle small. Performance budget, not detail.
const CommandDeck = lazy(() => import("./pages/CommandDeck"));
const SnipeFeed = lazy(() => import("./pages/SnipeFeed"));
const Candidates = lazy(() => import("./pages/Candidates"));
const WalletClusters = lazy(() => import("./pages/WalletClusters"));
const Latency = lazy(() => import("./pages/Latency"));
const Positions = lazy(() => import("./pages/Positions"));
const CopyTrade = lazy(() => import("./pages/CopyTrade"));
const Sentiment = lazy(() => import("./pages/Sentiment"));
const Narrative = lazy(() => import("./pages/Narrative"));
const Model = lazy(() => import("./pages/Model"));
const Reasoning = lazy(() => import("./pages/Reasoning"));
const Risk = lazy(() => import("./pages/Risk"));
const Monitoring = lazy(() => import("./pages/Monitoring"));
const Settings = lazy(() => import("./pages/Settings"));

function RouteFallback() {
  return (
    <div className="flex h-full items-center justify-center text-faint">
      <span className="num text-[12px] tracking-wide">Loading…</span>
    </div>
  );
}

/**
 * Single Layout instance for the whole app. The sidebar/topbar chrome mounts
 * once here and stays mounted across navigation; only the routed page swaps
 * inside Layout's <Outlet>. Suspense lives *inside* the chrome, so a lazy page
 * load shows the small fallback in the content area — the deck never blanks.
 */
function AppLayout() {
  return (
    <Layout>
      <Suspense fallback={<RouteFallback />}>
        <Outlet />
      </Suspense>
    </Layout>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<CommandDeck />} />
        <Route path="/feed" element={<SnipeFeed />} />
        <Route path="/candidates" element={<Candidates />} />
        <Route path="/wallet-clusters" element={<WalletClusters />} />
        <Route path="/latency" element={<Latency />} />
        <Route path="/positions" element={<Positions />} />
        <Route path="/copy-trade" element={<CopyTrade />} />
        <Route path="/sentiment" element={<Sentiment />} />
        <Route path="/narrative" element={<Narrative />} />
        <Route path="/model" element={<Model />} />
        <Route path="/reasoning" element={<Reasoning />} />
        <Route path="/risk" element={<Risk />} />
        <Route path="/monitoring" element={<Monitoring />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
    </Routes>
  );
}
