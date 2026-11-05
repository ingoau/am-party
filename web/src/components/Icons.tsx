import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };
const base = (size = 20, p: P) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  ...p,
});

export const IconPlay = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M7 4.5v15l12-7.5z" fill="currentColor" stroke="none" /></svg>
);
export const IconPause = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><rect x="6" y="4.5" width="4" height="15" rx="1" fill="currentColor" stroke="none" /><rect x="14" y="4.5" width="4" height="15" rx="1" fill="currentColor" stroke="none" /></svg>
);
export const IconSkip = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M5 4.5v15l10-7.5z" fill="currentColor" stroke="none" /><path d="M19 5v14" strokeWidth={2.5} /></svg>
);
export const IconVolume = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" fill="currentColor" stroke="none" /><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11" /></svg>
);
export const IconMute = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" fill="currentColor" stroke="none" /><path d="M16 9.5l5 5M21 9.5l-5 5" /></svg>
);
export const IconHeadphones = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M4 15v-3a8 8 0 0 1 16 0v3" /><rect x="3" y="14" width="4.5" height="7" rx="1.5" fill="currentColor" /><rect x="16.5" y="14" width="4.5" height="7" rx="1.5" fill="currentColor" /></svg>
);
export const IconSettings = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></svg>
);
export const IconUsers = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 14a6.5 6.5 0 0 1 3 6" /></svg>
);
export const IconSearch = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></svg>
);
export const IconCheck = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
);
export const IconX = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M6 6l12 12M18 6L6 18" /></svg>
);
export const IconUp = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M6 15l6-6 6 6" /></svg>
);
export const IconDown = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M6 9l6 6 6-6" /></svg>
);
export const IconTop = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M6 17l6-6 6 6M6 7h12" /></svg>
);
export const IconTrash = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></svg>
);
export const IconPlus = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M12 5v14M5 12h14" /></svg>
);
export const IconNext = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M4 12h11M11 6l6 6-6 6" /><path d="M20 5v14" /></svg>
);
export const IconMusic = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M9 18V5l11-2v13" /><circle cx="6" cy="18" r="3" fill="currentColor" /><circle cx="17" cy="16" r="3" fill="currentColor" /></svg>
);
export const IconQueue = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M4 6h12M4 12h12M4 18h8" /><path d="M18 14v6l4-3z" fill="currentColor" /></svg>
);
export const IconInbox = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M3 13l3-8h12l3 8v6H3z" /><path d="M3 13h5l1.5 2.5h5L16 13h5" /></svg>
);
export const IconLogout = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10" /></svg>
);
export const IconTv = ({ size, ...p }: P) => (
  <svg {...base(size, p)}><rect x="2.5" y="5" width="19" height="13" rx="2" /><path d="M8 21h8" /></svg>
);
export const IconExplicit = ({ size, ...p }: P) => (
  <svg {...base(size, p)} viewBox="0 0 16 16"><rect x="1" y="1" width="14" height="14" rx="2.5" fill="currentColor" stroke="none" /><path d="M10.5 4.5h-5v7h5M5.5 8h4" stroke="var(--bg, #000)" strokeWidth={1.6} /></svg>
);
