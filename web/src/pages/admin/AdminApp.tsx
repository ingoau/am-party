import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router";
import { api, ApiError } from "../../api/client";
import type { Me, User } from "../../api/types";
import { useLive, useLiveEvent } from "../../hooks/live";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { Inbox, ListMusic, LogOut, Settings, Tv, Users } from "lucide-react";
import { Toaster } from "../../components/Toaster";
import { RequestsTab } from "./RequestsTab";
import { QueueTab } from "./QueueTab";
import { UsersTab } from "./UsersTab";
import { SettingsTab } from "./SettingsTab";
import { can, canQueueTab } from "./permissions";
import "../../styles/admin.css";

type Section = { path: string; label: string; icon: ReactNode; allowed: (u: User) => boolean };

const SECTIONS: Section[] = [
  { path: "requests", label: "Requests", icon: <Inbox size={20} />, allowed: (u) => can(u, "approve_requests") },
  { path: "queue", label: "Queue", icon: <ListMusic size={20} />, allowed: canQueueTab },
  { path: "users", label: "People", icon: <Users size={20} />, allowed: (u) => u.is_admin },
  { path: "settings", label: "Settings", icon: <Settings size={20} />, allowed: (u) => u.is_admin },
];

export default function AdminApp() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { state } = useLive();

  const load = useCallback(() => {
    api
      .me()
      .then((m) => {
        setMe(m);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load your account."));
  }, []);
  useEffect(load, [load]);
  // Sent to every signed-in user whenever anyone's permissions change — ours may have too.
  useLiveEvent("users_changed", load);

  useDocumentTitle(`Host · ${state?.party_name || "am-party"}`);

  if (error && !me) {
    return (
      <Gate title="Can't reach the server" body={error}>
        <button className="btn btn--primary" onClick={load}>
          Try again
        </button>
      </Gate>
    );
  }
  if (!me) {
    return (
      <div className="center-screen">
        <div className="spinner" />
      </div>
    );
  }
  if (!me.user) {
    return (
      <Gate
        title={state?.party_name || "am-party"}
        body={me.oidc_enabled ? "Hosts sign in to approve requests and run the queue." : "Sign-in isn't configured on this server. Set up OIDC to use the host panel."}
      >
        {me.oidc_enabled && (
          <a className="btn btn--primary btn--lg" href={`/auth/login?next=${encodeURIComponent("/admin")}`}>
            Sign in with {me.oidc_name || "SSO"}
          </a>
        )}
        <a className="btn btn--ghost" href="/">
          Back to the party
        </a>
      </Gate>
    );
  }

  const user = me.user;
  const allowed = SECTIONS.filter((s) => s.allowed(user));

  if (allowed.length === 0) {
    return (
      <Gate title="Waiting for access" body={`You're signed in as ${user.name}. An admin needs to give you access. This page updates on its own.`}>
        <button className="btn btn--ghost" onClick={signOut}>
          Sign out
        </button>
      </Gate>
    );
  }

  return (
    <div className="admin">
      <aside className="admin__nav">
        <div className="admin__brand">
          <span className="brand__mark" aria-hidden>
            <span />
            <span />
            <span />
          </span>
          <div className="admin__party truncate">{state?.party_name || "am-party"}</div>
        </div>
        <nav className="admin__links">
          {allowed.map((s) => (
            <NavLink key={s.path} to={`/admin/${s.path}`} className={({ isActive }) => "admin__link" + (isActive ? " is-active" : "")}>
              {s.icon}
              <span>{s.label}</span>
            </NavLink>
          ))}
          <a className="admin__link" href="/" target="_blank" rel="noreferrer">
            <Tv size={20} />
            <span>TV view</span>
          </a>
        </nav>
        <div className="admin__me">
          {user.picture ? (
            <img className="avatar" src={user.picture} alt="" referrerPolicy="no-referrer" />
          ) : (
            <span className="avatar avatar--initials">{initials(user.name)}</span>
          )}
          <div className="admin__me-text">
            <div className="truncate">{user.name}</div>
            <div className="muted truncate">{user.email ?? ""}</div>
          </div>
          <button className="icon-btn" onClick={signOut} aria-label="Sign out" title="Sign out">
            <LogOut size={20} />
          </button>
        </div>
      </aside>

      <main className="admin__main">
        <Routes>
          <Route index element={<Navigate to={`/admin/${allowed[0].path}`} replace />} />
          {can(user, "approve_requests") && <Route path="requests/*" element={<RequestsTab />} />}
          {canQueueTab(user) && <Route path="queue" element={<QueueTab me={user} />} />}
          {user.is_admin && <Route path="users" element={<UsersTab me={user} />} />}
          {user.is_admin && <Route path="settings" element={<SettingsTab />} />}
          <Route path="*" element={<Navigate to={`/admin/${allowed[0].path}`} replace />} />
        </Routes>
        <Toaster />
      </main>
    </div>
  );
}

async function signOut() {
  try {
    await api.logout();
  } catch {
    /* ignore */
  }
  location.href = "/admin";
}

export function initials(name: string) {
  return (
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((p) => p[0]!.toUpperCase())
      .join("") || "?"
  );
}

function Gate({ title, body, children }: { title: string; body: string; children?: ReactNode }) {
  return (
    <div className="gate">
      <div className="gate__card">
        <span className="brand__mark brand__mark--lg" aria-hidden>
          <span />
          <span />
          <span />
        </span>
        <h1>{title}</h1>
        <p>{body}</p>
        <div className="gate__actions">{children}</div>
      </div>
    </div>
  );
}
