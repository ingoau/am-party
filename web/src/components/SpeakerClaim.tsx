import { useEffect, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router";
import { Speaker as SpeakerIcon, X } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { SpeakerBrief } from "../api/types";
import { can } from "../pages/admin/permissions";

type Claim =
  | { kind: "idle" }
  | { kind: "signin"; provider: string }
  | { kind: "ask"; speaker: SpeakerBrief }
  | { kind: "claiming"; speaker: SpeakerBrief }
  | { kind: "done"; speaker: SpeakerBrief; already: boolean }
  | { kind: "error"; message: string };

/**
 * `/?speaker=<claim token>` (a speaker's QR): people who may claim speakers get a claim prompt,
 * signed-out visitors a small sign-in link; everyone else just sees the request page.
 */
export function SpeakerClaim() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const token = params.get("speaker");
  const [claim, setClaim] = useState<Claim>({ kind: "idle" });

  useEffect(() => {
    if (!token) return setClaim({ kind: "idle" });
    let alive = true;
    (async () => {
      let me: Awaited<ReturnType<typeof api.me>>;
      try {
        me = await api.me();
      } catch {
        // can't tell who this is (flaky wifi): guests just get the normal page
        if (alive) setClaim({ kind: "idle" });
        return;
      }
      try {
        if (!alive) return;
        if (!me.user) {
          setClaim(me.oidc_enabled ? { kind: "signin", provider: me.oidc_name || "SSO" } : { kind: "idle" });
          return;
        }
        if (!can(me.user, "claim_speakers")) return setClaim({ kind: "idle" });
        const r = await api.claimPreview(token);
        if (!alive) return;
        setClaim(r.already_mine ? { kind: "done", speaker: r.speaker, already: true } : { kind: "ask", speaker: r.speaker });
      } catch (e) {
        if (!alive) return;
        setClaim({
          kind: "error",
          message:
            e instanceof ApiError && e.status === 404
              ? "This speaker code has expired. Scan the code on the speaker again."
              : e instanceof ApiError
                ? e.message
                : "Couldn't check this speaker.",
        });
      }
    })();
    return () => {
      alive = false;
    };
  }, [token]);

  const dismiss = () => {
    const next = new URLSearchParams(params);
    next.delete("speaker");
    setParams(next, { replace: true });
  };

  const doClaim = async (speaker: SpeakerBrief) => {
    if (!token) return;
    setClaim({ kind: "claiming", speaker });
    try {
      const r = await api.claimSpeaker(token);
      setClaim({ kind: "done", speaker: { id: r.speaker.id, name: r.speaker.name, connected: r.speaker.connected }, already: false });
    } catch (e) {
      setClaim({
        kind: "error",
        message: e instanceof ApiError && e.status === 404 ? "This speaker code has expired. Scan the code on the speaker again." : e instanceof ApiError ? e.message : "Couldn't claim this speaker.",
      });
    }
  };

  if (claim.kind === "idle") return null;

  if (claim.kind === "signin") {
    return (
      <p className="claim-signin">
        Own this speaker?{" "}
        <a href={`/auth/login?next=${encodeURIComponent(location.pathname + location.search)}`}>Sign in to claim it</a>
      </p>
    );
  }

  return (
    <section className="claim" aria-live="polite">
      <SpeakerIcon className="claim__icon" size={22} aria-hidden />
      <div className="claim__body">
        {claim.kind === "error" ? (
          <p className="claim__text">{claim.message}</p>
        ) : claim.kind === "done" ? (
          <p className="claim__text">
            {claim.already ? (
              <>“{claim.speaker.name}” is already one of your speakers.</>
            ) : (
              <>“{claim.speaker.name}” is now one of your speakers.</>
            )}{" "}
            <Link to="/admin/speakers">Manage speakers</Link>
          </p>
        ) : (
          <>
            <p className="claim__text">
              <strong>Claim speaker “{claim.speaker.name}”?</strong>
              <br />
              You'll be able to control its volume from your speaker list.
            </p>
            <div className="claim__actions">
              <button type="button" className="btn btn--primary btn--sm" disabled={claim.kind === "claiming"} onClick={() => doClaim(claim.speaker)}>
                {claim.kind === "claiming" ? "Claiming…" : "Claim"}
              </button>
              <button type="button" className="btn btn--ghost btn--sm" onClick={dismiss}>
                Not now
              </button>
            </div>
          </>
        )}
      </div>
      {claim.kind !== "ask" && claim.kind !== "claiming" && (
        <button type="button" className="icon-btn claim__close" onClick={dismiss} aria-label="Dismiss">
          <X size={18} />
        </button>
      )}
    </section>
  );
}
