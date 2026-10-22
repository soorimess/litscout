import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import JobPage from "./pages/JobPage";
import JobsPage from "./pages/JobsPage";
import LoginPage from "./pages/LoginPage";
import NewJobPage from "./pages/NewJobPage";

function TopBar() {
  const { user, signOut } = useAuth();
  return (
    <header className="topbar">
      <div className="topbar-inner">
        <NavLink to="/" className="brand">
          Lit<span>Scout</span>
        </NavLink>
        <nav aria-label="Main">
          <NavLink to="/" end>
            Reviews
          </NavLink>
          <NavLink to="/new">New review</NavLink>
        </nav>
        <span className="muted small">{user}</span>
        <button onClick={() => void signOut()}>Sign out</button>
      </div>
    </header>
  );
}

export default function App() {
  const { user, checking } = useAuth();
  const location = useLocation();
  if (checking) return <main className="muted">Loading…</main>;
  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace state={{ from: location.pathname }} />} />
      </Routes>
    );
  }
  return (
    <>
      <TopBar />
      <main>
        <Routes>
          <Route path="/" element={<JobsPage />} />
          <Route path="/new" element={<NewJobPage />} />
          <Route path="/jobs/:id" element={<JobPage />} />
          <Route path="/login" element={<Navigate to="/" replace />} />
          <Route path="*" element={<p>Page not found.</p>} />
        </Routes>
      </main>
    </>
  );
}
