import { lazy, Suspense } from "react";
import { Routes, Route } from "react-router";

// Routes are code-split: each page (and the heavy recharts it pulls in) loads
// on demand, keeping the initial bundle small. Performance budget, not detail.
const CommandDeck = lazy(() => import("./pages/CommandDeck"));
const SnipeFeed = lazy(() => import("./pages/SnipeFeed"));
const Latency = lazy(() => import("./pages/Latency"));
const Positions = lazy(() => import("./pages/Positions"));
const Sentiment = lazy(() => import("./pages/Sentiment"));
const Model = lazy(() => import("./pages/Model"));
const Reasoning = lazy(() => import("./pages/Reasoning"));
const Risk = lazy(() => import("./pages/Risk"));
const Monitoring = lazy(() => import("./pages/Monitoring"));
const Settings = lazy(() => import("./pages/Settings"));

function RouteFallback() {
  return (
    <div className="flex h-screen items-center justify-center bg-bg text-faint">
      <span className="num text-[12px] tracking-wide">Loading…</span>
    </div>
  );
}

export default function App() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route path="/" element={<CommandDeck />} />
        <Route path="/feed" element={<SnipeFeed />} />
        <Route path="/latency" element={<Latency />} />
        <Route path="/positions" element={<Positions />} />
        <Route path="/sentiment" element={<Sentiment />} />
        <Route path="/model" element={<Model />} />
        <Route path="/reasoning" element={<Reasoning />} />
        <Route path="/risk" element={<Risk />} />
        <Route path="/monitoring" element={<Monitoring />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </Suspense>
  );
}
