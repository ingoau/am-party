import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { User } from "../../api/types";
import { useLiveEvent } from "../../hooks/live";
import { Trash2 } from "lucide-react";
import { initials } from "./AdminApp";
import { PageHeader, flash } from "./common";

type Perm = "can_approve" | "can_manage_queue" | "is_admin";
const PERMS: { key: Perm; label: string; hint: string }[] = [
  { key: "can_approve", label: "Approve", hint: "Approve or reject guest requests" },
  { key: "can_manage_queue", label: "Queue", hint: "Reorder, skip, pause and add songs" },
  { key: "is_admin", label: "Admin", hint: "Manage people and settings" },
];

export function UsersTab({ me }: { me: User }) {
  const [users, setUsers] = useState<User[] | null>(null);

  const load = useCallback(() => {
    api
      .users()
      .then((r) => setUsers(r.users))
      .catch(flash.err);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(load, [load]);
  useLiveEvent("users_changed", load);

  const toggle = async (u: User, key: Perm) => {
    const value = !u[key];
    setUsers((list) => list?.map((x) => (x.id === u.id ? { ...x, [key]: value } : x)) ?? list);
    try {
      await api.patchUser(u.id, { [key]: value });
    } catch (e) {
      flash.err(e);
    } finally {
      load();
    }
  };

  const remove = async (u: User) => {
    if (!window.confirm(`Remove ${u.name}? They can sign in again but will have no access.`)) return;
    try {
      await api.deleteUser(u.id);
      flash.ok(`Removed ${u.name}`);
    } catch (e) {
      flash.err(e);
    } finally {
      load();
    }
  };

  const pending = users?.filter((u) => !u.is_admin && !u.can_approve && !u.can_manage_queue).length ?? 0;

  return (
    <div className="page">
      <PageHeader
        title="People"
        sub={pending > 0 ? `${pending} ${pending === 1 ? "person is" : "people are"} waiting for access.` : "Everyone who has signed in to the host panel."}
      />
      {users === null ? (
        <div className="spinner" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Person</th>
                {PERMS.map((p) => (
                  <th key={p.key} title={p.hint} className="table__perm">
                    {p.label}
                  </th>
                ))}
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>
                    <div className="person">
                      {u.picture ? (
                        <img className="avatar" src={u.picture} alt="" referrerPolicy="no-referrer" />
                      ) : (
                        <span className="avatar avatar--initials">{initials(u.name)}</span>
                      )}
                      <div className="person__text">
                        <div className="truncate">
                          {u.name}
                          {u.id === me.id && <span className="muted"> (you)</span>}
                        </div>
                        <div className="muted truncate">{u.email ?? ""}</div>
                      </div>
                    </div>
                  </td>
                  {PERMS.map((p) => (
                    <td key={p.key} className="table__perm" data-label={p.label}>
                      <label className="switch-only" title={p.hint}>
                        <input
                          type="checkbox"
                          checked={u[p.key]}
                          onChange={() => toggle(u, p.key)}
                          aria-label={`${p.label} for ${u.name}`}
                        />
                        <span className="switch" aria-hidden />
                      </label>
                    </td>
                  ))}
                  <td className="table__actions">
                    <button
                      className="icon-btn icon-btn--danger"
                      onClick={() => remove(u)}
                      disabled={u.id === me.id}
                      aria-label={`Remove ${u.name}`}
                      title={u.id === me.id ? "You can't remove yourself" : "Remove"}
                    >
                      <Trash2 size={18} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="hint">New people appear here after they sign in. The first person to sign in becomes an admin.</p>
    </div>
  );
}
