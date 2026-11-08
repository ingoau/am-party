import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { ChevronDown, Trash2, X } from "lucide-react";
import { api } from "../../api/client";
import { ALL_PERMISSIONS, type Permission, type PermissionInfo, type User } from "../../api/types";
import { useLiveEvent } from "../../hooks/live";
import { cx, timeAgo } from "../../util";
import { initials } from "./AdminApp";
import { Checkbox, PageHeader, flash } from "./common";
import { useConfirm } from "./ConfirmDialog";
import { APPROVE_PRESET, PRESETS, ROLE_LABELS, groupCatalog, usePermissionCatalog, type PermissionGroup, type Preset } from "./permissions";

const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

export function UsersTab({ me }: { me: User }) {
  const [users, setUsers] = useState<User[] | null>(null);
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const toggleExpanded = (id: number) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  const catalog = usePermissionCatalog();
  const groups = useMemo(() => groupCatalog(catalog), [catalog]);
  const [confirmDialog, confirm] = useConfirm();

  // Only the newest /users response applies, and none while a toggle is still in flight
  // (that response could predate the change and flip the optimistic switch back).
  const loadSeq = useRef(0);
  const inFlight = useRef(0);
  const load = useCallback(() => {
    const seq = ++loadSeq.current;
    api
      .users()
      .then((r) => {
        if (seq === loadSeq.current && inFlight.current === 0) setUsers(r.users);
      })
      .catch(flash.err);
  }, []);
  /** Run one per-user change: optimistic patch, then a delta request (never a full set built from a stale render). */
  const mutate = async (fn: () => Promise<unknown>, okMsg?: string) => {
    inFlight.current++;
    try {
      await fn();
      if (okMsg) flash.ok(okMsg);
    } catch (e) {
      flash.err(e);
    } finally {
      inFlight.current--;
      load();
    }
  };
  useEffect(load, [load]);
  useLiveEvent("users_changed", load);

  // Forget selections for people who no longer exist.
  useEffect(() => {
    if (!users) return;
    setSelected((sel) => {
      const ids = new Set(users.map((u) => u.id));
      const next = new Set([...sel].filter((id) => ids.has(id)));
      return next.size === sel.size ? sel : next;
    });
  }, [users]);

  const patchLocal = (id: number, fn: (u: User) => User) => setUsers((list) => list?.map((u) => (u.id === id ? fn(u) : u)) ?? list);

  const togglePerm = (u: User, p: Permission) => {
    if (u.is_admin) return;
    const on = !u.granted.includes(p);
    patchLocal(u.id, (x) => {
      const granted = on ? [...new Set([...x.granted, p])] : x.granted.filter((g) => g !== p);
      return { ...x, granted, permissions: granted };
    });
    void mutate(() => api.bulkUsers({ user_ids: [u.id], ...(on ? { add: [p] } : { remove: [p] }) }));
  };

  /** One-tap access for someone waiting for it. */
  const grantPreset = (u: User, preset: Preset) => {
    patchLocal(u.id, (x) => {
      const granted = [...new Set([...x.granted, ...preset.permissions])];
      return { ...x, granted, permissions: granted };
    });
    void mutate(
      () => api.bulkUsers({ user_ids: [u.id], add: [...preset.permissions] }),
      `${u.name} approved`,
    );
  };

  const toggleAdmin = async (u: User) => {
    const value = !u.is_admin;
    if (!value && u.id === me.id) {
      const ok = await confirm({
        title: "Remove your own admin access?",
        description: "You'll lose People and Settings straight away. Another admin will have to give it back.",
        confirmLabel: "Remove my admin access",
        danger: true,
      });
      if (!ok) return;
    }
    patchLocal(u.id, (x) => ({ ...x, is_admin: value, permissions: value ? [...ALL_PERMISSIONS] : x.granted }));
    void mutate(() => api.bulkUsers({ user_ids: [u.id], is_admin: value }));
  };

  const remove = async (u: User) => {
    const ok = await confirm({
      title: `Remove ${u.name}?`,
      description: "They'll lose all host access. They can sign in again later, but will have to wait for someone to grant them access.",
      confirmLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteUser(u.id);
      flash.ok(`Removed ${u.name}`);
    } catch (e) {
      flash.err(e);
    } finally {
      load();
    }
  };

  const selectedUsers = users?.filter((u) => selected.has(u.id)) ?? [];
  const bulk = async (body: { add?: Permission[]; remove?: Permission[]; set?: Permission[]; is_admin?: boolean }, okMsg: string) => {
    if (selectedUsers.length === 0) return;
    setBulkBusy(true);
    try {
      await api.bulkUsers({ user_ids: selectedUsers.map((u) => u.id), ...body });
      flash.ok(okMsg);
    } catch (e) {
      flash.err(e);
    } finally {
      setBulkBusy(false);
      load();
    }
  };
  const who = (n = selectedUsers.length) => plural(n, "person", "people");

  const bulkMakeAdmin = async () => {
    const n = selectedUsers.length;
    const ok = await confirm({
      title: n === 1 ? `Make ${selectedUsers[0]!.name} an admin?` : `Make ${n} people admins?`,
      description: "Admins have every permission and can manage people and settings, including removing other admins.",
      confirmLabel: n === 1 ? "Make admin" : `Make ${n} admins`,
    });
    if (ok) void bulk({ is_admin: true }, `${who(n)} made admin`);
  };
  const bulkRemoveAdmin = async () => {
    if (selected.has(me.id)) {
      const ok = await confirm({
        title: "Remove admin, including yourself?",
        description: "Your selection includes you. You'll lose People and Settings straight away.",
        confirmLabel: "Remove admin",
        danger: true,
      });
      if (!ok) return;
    }
    void bulk({ is_admin: false }, `Removed admin from ${who()}`);
  };

  const toggleSelect = (id: number) =>
    setSelected((sel) => {
      const next = new Set(sel);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  const allSelected = !!users?.length && users.every((u) => selected.has(u.id));
  const someSelected = selected.size > 0 && !allSelected;
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(users?.map((u) => u.id)));

  const isWaiting = (u: User) => !u.is_admin && u.granted.length === 0;
  const waiting = users?.filter(isWaiting) ?? [];
  const team = users?.filter((u) => !isWaiting(u)) ?? [];
  const row = (u: User) => (
    <UserRow
      key={u.id}
      user={u}
      isMe={u.id === me.id}
      groups={groups}
      selected={selected.has(u.id)}
      expanded={expanded.has(u.id)}
      onExpand={() => toggleExpanded(u.id)}
      onSelect={() => toggleSelect(u.id)}
      onToggle={(p) => togglePerm(u, p)}
      onToggleAdmin={() => toggleAdmin(u)}
      onPreset={(preset) => grantPreset(u, preset)}
      onRemove={() => remove(u)}
    />
  );

  return (
    <>
      <div className="page page--people">
        <PageHeader title="People" />
        {users === null ? (
          <div className="spinner" />
        ) : (
          <div className="people">
            <div className="people__toolbar">
              <label className="people__select-all">
                <Checkbox checked={allSelected} indeterminate={someSelected} onChange={toggleAll} aria-label="Select everyone" />
                <span>{selected.size > 0 ? `${selected.size} of ${users.length} selected` : "Select all"}</span>
              </label>
              <PermissionGuide groups={groups} />
            </div>

            {waiting.length > 0 && (
              <>
                <h2 className="section-title">
                  Waiting for access <span className="muted">{waiting.length}</span>
                </h2>
                <ul className="ulist" aria-label="Waiting for access">
                  {waiting.map(row)}
                </ul>
                {team.length > 0 && <h2 className="section-title">Team</h2>}
              </>
            )}
            {team.length > 0 && (
              <ul className="ulist" aria-label="Team">
                {team.map(row)}
              </ul>
            )}
          </div>
        )}
        <p className="hint">People appear here after they sign in.</p>

        {selectedUsers.length > 0 && (
          <div className="bulk-bar" role="region" aria-label="Bulk actions">
            <div className="bulk-bar__count">
              <strong>{selectedUsers.length}</strong> selected
            </div>
            <div className="bulk-bar__actions">
              <PermMenu
                label="Grant"
                groups={groups}
                disabled={bulkBusy}
                presets
                onPick={(perms, name) => bulk({ add: perms }, `Granted ${name} to ${who()}`)}
              />
              <PermMenu
                label="Revoke"
                groups={groups}
                disabled={bulkBusy}
                onPick={(perms, name) =>
                  perms.length === catalog.length
                    ? bulk({ set: [] }, `Revoked all permissions from ${who()}`)
                    : bulk({ remove: perms }, `Revoked ${name} from ${who()}`)
                }
                allLabel="All permissions"
                allPerms={catalog.map((c) => c.id)}
              />
              <div className="bulk-bar__admin">
                <button className="btn btn--sm" disabled={bulkBusy} onClick={bulkMakeAdmin}>
                  Make admin
                </button>
                <button className="btn btn--sm" disabled={bulkBusy} onClick={bulkRemoveAdmin}>
                  Remove admin
                </button>
              </div>
              <DropdownMenu.Root>
                <DropdownMenu.Trigger asChild>
                  <button className="btn btn--sm bulk-bar__admin-menu" disabled={bulkBusy}>
                    Admin <ChevronDown size={14} aria-hidden />
                  </button>
                </DropdownMenu.Trigger>
                <DropdownMenu.Portal>
                  <DropdownMenu.Content className="menu" side="top" align="end" sideOffset={8} collisionPadding={12}>
                    <DropdownMenu.Item className="menu__item" onSelect={bulkMakeAdmin}>
                      Make admin
                    </DropdownMenu.Item>
                    <DropdownMenu.Item className="menu__item" onSelect={bulkRemoveAdmin}>
                      Remove admin
                    </DropdownMenu.Item>
                  </DropdownMenu.Content>
                </DropdownMenu.Portal>
              </DropdownMenu.Root>
            </div>
            <button className="icon-btn bulk-bar__clear" onClick={() => setSelected(new Set())} aria-label="Clear selection" title="Clear selection">
              <X size={18} />
            </button>
          </div>
        )}
        {confirmDialog}
      </div>
    </>
  );
}

function UserRow({
  user: u,
  isMe,
  groups,
  selected,
  expanded,
  onExpand,
  onSelect,
  onToggle,
  onToggleAdmin,
  onPreset,
  onRemove,
}: {
  user: User;
  isMe: boolean;
  groups: PermissionGroup[];
  selected: boolean;
  expanded: boolean;
  onExpand: () => void;
  onSelect: () => void;
  onToggle: (p: Permission) => void;
  onToggleAdmin: () => void;
  onPreset: (p: Preset) => void;
  onRemove: () => void;
}) {
  const role = roleOf(u);
  const waiting = !u.is_admin && u.granted.length === 0;
  const panelId = `perms-${u.id}`;
  return (
    <li className={cx("urow", selected && "is-selected", expanded && "is-expanded", waiting && "is-waiting")}>
      <div className="urow__select">
        <Checkbox checked={selected} onChange={onSelect} aria-label={`Select ${u.name}`} />
      </div>
      <div className="urow__person person">
        {u.picture ? (
          <img className="avatar" src={u.picture} alt="" referrerPolicy="no-referrer" />
        ) : (
          <span className="avatar avatar--initials">{initials(u.name)}</span>
        )}
        <div className="person__text">
          <div className="person__name">
            <span className="truncate">{u.name}</span>
            {isMe && <span className="muted person__you">you</span>}
          </div>
          <div className="muted truncate">
            {u.email ?? ""}
            {u.last_login ? <span className="person__seen"> · seen {timeAgo(u.last_login)}</span> : null}
          </div>
        </div>
      </div>
      {waiting && (
        <div className="urow__quick">
          <button type="button" className="btn btn--sm" onClick={() => onPreset(APPROVE_PRESET)} title={APPROVE_PRESET.description} aria-label={`Approve ${u.name}`}>
            Approve
          </button>
        </div>
      )}
      <button
        type="button"
        className={cx("role-btn", role.className)}
        onClick={onExpand}
        aria-expanded={expanded}
        aria-controls={panelId}
        aria-label={`${role.label}. Edit permissions for ${u.name}`}
        title="Edit permissions"
      >
        <span>{role.label}</span>
        <ChevronDown size={14} className="role-btn__chev" aria-hidden />
      </button>
      <div className="urow__actions">
        <button
          className="icon-btn icon-btn--danger"
          onClick={onRemove}
          disabled={isMe}
          aria-label={`Remove ${u.name}`}
          title={isMe ? "You can't remove yourself" : "Remove"}
        >
          <Trash2 size={18} />
        </button>
      </div>
      {expanded && (
        <div className="urow__perms" id={panelId}>
          {groups.map((g) => (
            <div className="urow__group" key={g.name}>
              <div className="urow__group-name">{g.name}</div>
              <div className="urow__pills">
                {g.items.map((p) => (
                  <PermSwitch
                    key={p.id}
                    info={p}
                    checked={u.is_admin || u.granted.includes(p.id)}
                    disabled={u.is_admin}
                    onChange={() => onToggle(p.id)}
                    who={u.name}
                  />
                ))}
              </div>
            </div>
          ))}
          <div className="urow__group">
            <div className="urow__group-name">Access</div>
            <div className="urow__pills">
              <PermSwitch
                info={{ label: "Admin", description: "Every permission, plus people and settings" }}
                checked={u.is_admin}
                onChange={onToggleAdmin}
                who={u.name}
                admin
              />
            </div>
          </div>
        </div>
      )}
    </li>
  );
}

function roleOf(u: User): { label: string; className: string } {
  if (u.is_admin) return { label: "Admin", className: "role-btn--admin" };
  if (u.granted.length === 0) return { label: "No access", className: "role-btn--none" };
  const set = new Set(u.granted);
  // Prefer the widest bundles, so "Approver" isn't also shown as "Request approver".
  const matched = ROLE_LABELS.filter((p) => p.permissions.every((x) => set.has(x))).filter(
    (p, _, all) => !all.some((q) => q !== p && q.permissions.length > p.permissions.length && p.permissions.every((x) => q.permissions.includes(x))),
  );
  const covered = new Set(matched.flatMap((p) => p.permissions));
  if (matched.length && u.granted.every((x) => covered.has(x))) return { label: matched.map((p) => p.label).join(" + "), className: "role-btn--role" };
  return { label: `Custom · ${u.granted.length}`, className: "role-btn--role" };
}

function PermSwitch({
  info,
  checked,
  disabled,
  onChange,
  who,
  admin,
}: {
  info: Pick<PermissionInfo, "label" | "description">;
  checked: boolean;
  disabled?: boolean;
  onChange: () => void;
  who: string;
  admin?: boolean;
}) {
  return (
    <label
      className={cx("perm", admin && "perm--admin", disabled && "is-disabled")}
      title={disabled ? `${info.label}: included with admin` : `${info.label}: ${info.description}`}
    >
      <span className="perm__label">{info.label}</span>
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={onChange} aria-label={`${info.label} for ${who}`} />
      <span className="switch switch--sm" aria-hidden />
    </label>
  );
}


/** "What do these mean?" — labels + descriptions for every permission, usable on touch devices too. */
function PermissionGuide({ groups }: { groups: PermissionGroup[] }) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button className="btn btn--ghost btn--sm">
          About permissions
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="menu menu--guide" align="end" sideOffset={8} collisionPadding={12}>
          {groups.map((g) => (
            <DropdownMenu.Group key={g.name}>
              <DropdownMenu.Label className="menu__label">{g.name}</DropdownMenu.Label>
              {g.items.map((p) => (
                <div key={p.id} className="menu__info">
                  <strong>{p.label}</strong>
                  <span>{p.description}</span>
                </div>
              ))}
            </DropdownMenu.Group>
          ))}
          <DropdownMenu.Group>
            <DropdownMenu.Label className="menu__label">Access</DropdownMenu.Label>
            <div className="menu__info">
              <strong>Admin</strong>
              <span>Every permission above, plus managing people and settings</span>
            </div>
          </DropdownMenu.Group>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function PermMenu({
  label,
  groups,
  disabled,
  presets,
  allLabel,
  allPerms,
  onPick,
}: {
  label: string;
  groups: PermissionGroup[];
  disabled?: boolean;
  presets?: boolean;
  allLabel?: string;
  allPerms?: Permission[];
  onPick: (perms: Permission[], name: string) => void;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button className="btn btn--sm" disabled={disabled}>
          {label} <ChevronDown size={14} aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="menu" side="top" align="start" sideOffset={8} collisionPadding={12}>
          {presets && (
            <>
              <DropdownMenu.Label className="menu__label">Presets</DropdownMenu.Label>
              {PRESETS.map((p) => (
                <DropdownMenu.Item key={p.id} className="menu__item menu__item--rich" onSelect={() => onPick(p.permissions, `“${p.label}”`)}>
                  <strong>{p.label}</strong>
                  <span>{p.description}</span>
                </DropdownMenu.Item>
              ))}
              <DropdownMenu.Separator className="menu__sep" />
            </>
          )}
          {allLabel && allPerms && (
            <>
              <DropdownMenu.Item className="menu__item" onSelect={() => onPick(allPerms, allLabel.toLowerCase())}>
                {allLabel}
              </DropdownMenu.Item>
              <DropdownMenu.Separator className="menu__sep" />
            </>
          )}
          {groups.map((g) => (
            <DropdownMenu.Group key={g.name}>
              <DropdownMenu.Label className="menu__label">{g.name}</DropdownMenu.Label>
              {g.items.map((p) => (
                <DropdownMenu.Item key={p.id} className="menu__item" onSelect={() => onPick([p.id], `“${p.label}”`)} title={p.description}>
                  {p.label}
                </DropdownMenu.Item>
              ))}
            </DropdownMenu.Group>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
