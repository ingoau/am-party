import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Tooltip from "@radix-ui/react-tooltip";
import { Check, ChevronDown, Info, Minus, Shield, ShieldCheck, ShieldOff, Trash2, UserMinus, UserPlus, X } from "lucide-react";
import { api } from "../../api/client";
import { ALL_PERMISSIONS, type Permission, type PermissionInfo, type User } from "../../api/types";
import { useLiveEvent } from "../../hooks/live";
import { cx, timeAgo } from "../../util";
import { initials } from "./AdminApp";
import { PageHeader, flash } from "./common";
import { useConfirm } from "./ConfirmDialog";
import { PRESETS, groupCatalog, usePermissionCatalog, type PermissionGroup } from "./permissions";

const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

export function UsersTab({ me }: { me: User }) {
  const [users, setUsers] = useState<User[] | null>(null);
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const catalog = usePermissionCatalog();
  const groups = useMemo(() => groupCatalog(catalog), [catalog]);
  const [confirmDialog, confirm] = useConfirm();

  const load = useCallback(() => {
    api
      .users()
      .then((r) => setUsers(r.users))
      .catch(flash.err);
  }, []);
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

  const togglePerm = async (u: User, p: Permission) => {
    if (u.is_admin) return;
    const on = !u.granted.includes(p);
    const granted = on ? [...u.granted, p] : u.granted.filter((x) => x !== p);
    patchLocal(u.id, (x) => ({ ...x, granted, permissions: granted }));
    try {
      await api.patchUser(u.id, { permissions: granted });
    } catch (e) {
      flash.err(e);
    } finally {
      load();
    }
  };

  const toggleAdmin = async (u: User) => {
    const value = !u.is_admin;
    if (!value && u.id === me.id) {
      const ok = await confirm({
        title: "Remove your own admin access?",
        description: "You'll lose People and Settings straight away. Another admin will have to give it back.",
        confirmLabel: "Remove my admin access",
        danger: true,
        icon: <ShieldOff size={22} />,
      });
      if (!ok) return;
    }
    patchLocal(u.id, (x) => ({ ...x, is_admin: value, permissions: value ? [...ALL_PERMISSIONS] : x.granted }));
    try {
      await api.patchUser(u.id, { is_admin: value });
    } catch (e) {
      flash.err(e);
    } finally {
      load();
    }
  };

  const remove = async (u: User) => {
    const ok = await confirm({
      title: `Remove ${u.name}?`,
      description: "They'll lose all host access. They can sign in again later, but will have to wait for someone to grant them access.",
      confirmLabel: "Remove",
      danger: true,
      icon: <Trash2 size={22} />,
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
      description: "Admins have every permission and can manage people and settings — including removing other admins.",
      confirmLabel: n === 1 ? "Make admin" : `Make ${n} admins`,
      icon: <ShieldCheck size={22} />,
    });
    if (ok) void bulk({ is_admin: true }, `${who(n)} made admin`);
  };
  const bulkRemoveAdmin = async () => {
    if (selected.has(me.id)) {
      const ok = await confirm({
        title: "Remove admin — including yourself?",
        description: "Your selection includes you. You'll lose People and Settings straight away.",
        confirmLabel: "Remove admin",
        danger: true,
        icon: <ShieldOff size={22} />,
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

  const waiting = users?.filter((u) => !u.is_admin && u.permissions.length === 0).length ?? 0;
  const nPermCols = catalog.length;

  return (
    <Tooltip.Provider delayDuration={250}>
      <div className="page page--people">
        <PageHeader
          title="People"
          sub={
            waiting > 0
              ? `${plural(waiting, "person is", "people are")} waiting for access.`
              : "Everyone who has signed in to the host panel."
          }
        />
        {users === null ? (
          <div className="spinner" />
        ) : (
          <div className="people" style={{ ["--perm-cols" as string]: nPermCols }}>
            <div className="people__toolbar">
              <label className="people__select-all">
                <Checkbox checked={allSelected} indeterminate={someSelected} onChange={toggleAll} aria-label="Select everyone" />
                <span>{selected.size > 0 ? `${selected.size} of ${users.length} selected` : `Select all ${users.length}`}</span>
              </label>
              <PermissionGuide groups={groups} />
            </div>

            <div className="ugrid-head" aria-hidden>
              <div className="ugrid-head__row ugrid-head__row--groups">
                <span />
                <span />
                {groups.map((g) => (
                  <span key={g.name} className="ugrid-head__group" style={{ gridColumn: `span ${g.items.length}` }}>
                    {g.name}
                  </span>
                ))}
                <span className="ugrid-head__group">Access</span>
                <span />
              </div>
              <div className="ugrid-head__row">
                <span />
                <span className="ugrid-head__label">Person</span>
                {groups.flatMap((g) => g.items).map((p) => (
                  <HeaderTip key={p.id} label={p.label} tip={p.description} />
                ))}
                <HeaderTip label="Admin" tip="Every permission, plus managing people and settings" />
                <span />
              </div>
            </div>

            <ul className="ugrid" aria-label="People">
              {users.map((u) => (
                <UserRow
                  key={u.id}
                  user={u}
                  isMe={u.id === me.id}
                  groups={groups}
                  selected={selected.has(u.id)}
                  onSelect={() => toggleSelect(u.id)}
                  onToggle={(p) => togglePerm(u, p)}
                  onToggleAdmin={() => toggleAdmin(u)}
                  onRemove={() => remove(u)}
                />
              ))}
            </ul>
          </div>
        )}
        <p className="hint">New people appear here after they sign in. The first person to sign in becomes an admin.</p>

        {selectedUsers.length > 0 && (
          <div className="bulk-bar" role="region" aria-label="Bulk actions">
            <div className="bulk-bar__count">
              <strong>{selectedUsers.length}</strong> selected
            </div>
            <div className="bulk-bar__actions">
              <PermMenu
                label="Grant"
                icon={<UserPlus size={16} />}
                groups={groups}
                disabled={bulkBusy}
                presets
                onPick={(perms, name) => bulk({ add: perms }, `Granted ${name} to ${who()}`)}
              />
              <PermMenu
                label="Revoke"
                icon={<UserMinus size={16} />}
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
                  <ShieldCheck size={16} /> Make admin
                </button>
                <button className="btn btn--sm" disabled={bulkBusy} onClick={bulkRemoveAdmin}>
                  <ShieldOff size={16} /> Remove admin
                </button>
              </div>
              <DropdownMenu.Root>
                <DropdownMenu.Trigger asChild>
                  <button className="btn btn--sm bulk-bar__admin-menu" disabled={bulkBusy}>
                    <Shield size={16} /> Admin <ChevronDown size={14} />
                  </button>
                </DropdownMenu.Trigger>
                <DropdownMenu.Portal>
                  <DropdownMenu.Content className="menu" side="top" align="end" sideOffset={8} collisionPadding={12}>
                    <DropdownMenu.Item className="menu__item" onSelect={bulkMakeAdmin}>
                      <ShieldCheck size={16} /> Make admin
                    </DropdownMenu.Item>
                    <DropdownMenu.Item className="menu__item" onSelect={bulkRemoveAdmin}>
                      <ShieldOff size={16} /> Remove admin
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
    </Tooltip.Provider>
  );
}

function UserRow({
  user: u,
  isMe,
  groups,
  selected,
  onSelect,
  onToggle,
  onToggleAdmin,
  onRemove,
}: {
  user: User;
  isMe: boolean;
  groups: PermissionGroup[];
  selected: boolean;
  onSelect: () => void;
  onToggle: (p: Permission) => void;
  onToggleAdmin: () => void;
  onRemove: () => void;
}) {
  const role = roleOf(u);
  return (
    <li className={cx("urow", selected && "is-selected", u.is_admin && "is-admin")}>
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
            <span className={cx("badge", role.className)}>{role.label}</span>
          </div>
          <div className="muted truncate">
            {u.email ?? ""}
            {u.last_login ? <span className="person__seen"> · seen {timeAgo(u.last_login)}</span> : null}
          </div>
        </div>
      </div>
      <div className="urow__perms">
        {u.is_admin && <p className="urow__admin-note">Admins have every permission.</p>}
        {groups.map((g) => (
          <div className="urow__group" key={g.name}>
            <div className="urow__group-name">{g.name}</div>
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
        ))}
        <div className="urow__group urow__group--admin">
          <div className="urow__group-name">Access</div>
          <PermSwitch
            info={{ label: "Admin", description: "Every permission, plus people and settings" }}
            checked={u.is_admin}
            onChange={onToggleAdmin}
            who={u.name}
            admin
          />
        </div>
      </div>
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
    </li>
  );
}

function roleOf(u: User): { label: string; className: string } {
  if (u.is_admin) return { label: "Admin", className: "badge--admin" };
  if (u.granted.length === 0) return { label: "No access", className: "badge--pending" };
  const set = new Set(u.granted);
  const matched = PRESETS.filter((p) => p.permissions.every((x) => set.has(x)));
  const covered = new Set(matched.flatMap((p) => p.permissions));
  if (matched.length && u.granted.every((x) => covered.has(x))) return { label: matched.map((p) => p.label).join(" + "), className: "badge--request" };
  return { label: `Custom · ${u.granted.length}`, className: "badge--custom" };
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
      title={disabled ? `${info.label} — included with admin` : `${info.label}: ${info.description}`}
    >
      <span className="perm__label">{info.label}</span>
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={onChange} aria-label={`${info.label} for ${who}`} />
      <span className="switch switch--sm" aria-hidden />
    </label>
  );
}

function Checkbox({
  checked,
  indeterminate,
  onChange,
  ...rest
}: {
  checked: boolean;
  indeterminate?: boolean;
  onChange: () => void;
  "aria-label": string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = !!indeterminate;
  }, [indeterminate]);
  return (
    <span className="check">
      <input ref={ref} type="checkbox" checked={checked} onChange={onChange} {...rest} />
      <span className="check__box" aria-hidden>
        {indeterminate ? <Minus size={14} strokeWidth={3} /> : checked ? <Check size={14} strokeWidth={3} /> : null}
      </span>
    </span>
  );
}

function HeaderTip({ label, tip }: { label: string; tip: string }) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <span className="ugrid-head__label ugrid-head__label--perm" tabIndex={-1}>
          {label}
        </span>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className="tooltip" side="top" sideOffset={6} collisionPadding={12}>
          {tip}
          <Tooltip.Arrow className="tooltip__arrow" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

/** "What do these mean?" — labels + descriptions for every permission, usable on touch devices too. */
function PermissionGuide({ groups }: { groups: PermissionGroup[] }) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button className="btn btn--ghost btn--sm">
          <Info size={16} /> What can each permission do?
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
  icon,
  groups,
  disabled,
  presets,
  allLabel,
  allPerms,
  onPick,
}: {
  label: string;
  icon: ReactNode;
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
          {icon} {label} <ChevronDown size={14} />
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
