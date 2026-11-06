import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router";

// Route-level code splitting: guests on / don't download the audio stack.
const TvPage = lazy(() => import("./pages/Tv").then((m) => ({ default: m.TvPage })));
const RequestPage = lazy(() => import("./pages/Request").then((m) => ({ default: m.RequestPage })));
const AdminApp = lazy(() => import("./pages/admin/AdminApp"));

const fallback = (
  <div className="center-screen">
    <div className="spinner" />
  </div>
);

export function App() {
  return (
    <Suspense fallback={fallback}>
      <Routes>
        <Route path="/" element={<RequestPage />} />
        <Route path="/tv" element={<TvPage />} />
        <Route path="/admin/*" element={<AdminApp />} />
        <Route path="*" element={<RequestPage />} />
      </Routes>
    </Suspense>
  );
}
