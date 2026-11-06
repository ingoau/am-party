import { useEffect, useState, type ReactNode } from "react";
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { restrictToParentElement, restrictToVerticalAxis } from "@dnd-kit/modifiers";
import { ArrowUpToLine, GripVertical, ListStart, Pause, Play, Plus, SkipForward, Trash2 } from "lucide-react";
import { api, ApiError } from "../../api/client";
import type { QueueItem, Track, User } from "../../api/types";
import { useLive } from "../../hooks/live";
import { useProgress } from "../../hooks/useProgress";
import { Artwork } from "../../components/Artwork";
import { ProgressBar } from "../../components/ProgressBar";
import { TrackRow } from "../../components/TrackRow";
import { cx } from "../../util";
import { PageHeader, flash } from "./common";
import { TrackSearch } from "./TrackSearch";
import { useConfirm } from "./ConfirmDialog";
import { can } from "./permissions";

export function QueueTab({ me }: { me: User }) {
  const { state, clockOffset, refresh } = useLive();
  const canAdd = can(me, "queue_add");
  const canReorder = can(me, "queue_reorder");
  const canRemove = can(me, "queue_remove");
  const canSkip = can(me, "playback_skip");
  const canPause = can(me, "playback_pause");
  const [confirmDialog, confirm] = useConfirm();
  const np = state?.now_playing ?? null;
  const paused = state?.paused ?? false;
  const position = useProgress(np, clockOffset, paused, 500);
  const [busy, setBusy] = useState(false);
  // Local optimistic ordering while a move is in flight.
  const [optimistic, setOptimistic] = useState<QueueItem[] | null>(null);
  const upcoming = optimistic ?? state?.upcoming ?? [];

  // Drop the optimistic order as soon as the server's view of the queue arrives (or after a grace period),
  // so a successful drag doesn't visibly snap back while the refresh is in flight.
  useEffect(() => setOptimistic(null), [state?.upcoming]);
  useEffect(() => {
    if (!optimistic) return;
    const t = window.setTimeout(() => setOptimistic(null), 4000);
    return () => window.clearTimeout(t);
  }, [optimistic]);

  const run = async (fn: () => Promise<unknown>, okMsg?: string, onErr: (e: unknown) => void = flash.err) => {
    setBusy(true);
    try {
      await fn();
      if (okMsg) flash.ok(okMsg);
    } catch (e) {
      setOptimistic(null);
      onErr(e);
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const move = (item: QueueItem, to: number) => {
    const from = upcoming.findIndex((u) => u.id === item.id);
    if (from < 0 || to < 0 || to >= upcoming.length || from === to) return;
    setOptimistic(arrayMove(upcoming, from, to));
    void run(
      () => api.queueMove(item.id, to),
      undefined,
      (e) =>
        e instanceof ApiError && e.status === 409
          ? flash.err(`“${item.track.title}” is already being mixed in, so it can't be moved right now.`)
          : flash.err(e),
    );
  };

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const item = upcoming.find((u) => u.id === active.id);
    const to = upcoming.findIndex((u) => u.id === over.id);
    if (item && to >= 0) move(item, to);
  };

  const skip = async () => {
    if (!np) return;
    const ok = await confirm({
      title: `Skip “${np.title}”?`,
      description: "This skips the song for everyone listening on all devices.",
      confirmLabel: "Skip for everyone",
      icon: <SkipForward size={22} fill="currentColor" />,
    });
    if (ok) void run(() => api.skip(), `Skipped “${np.title}”`);
  };

  const add = (t: Track, position: "next" | "end") =>
    run(() => api.queueAdd(t.id, position), position === "next" ? `“${t.title}” plays next` : `Added “${t.title}”`);

  return (
    <div className="page">
      <PageHeader title="Queue">
        {state && (
          <span className="muted page-header__meta">
            {state.listeners} {state.listeners === 1 ? "device" : "devices"} listening
          </span>
        )}
      </PageHeader>

      <section className="np-card">
        <Artwork url={np?.artwork_url} size={300} className="np-card__art" />
        <div className="np-card__body">
          {np ? (
            <>
              <div className="np-card__title" title={np.title}>
                {np.title}
              </div>
              <div className="np-card__artist truncate">
                {paused ? "Paused · " : ""}
                {np.artist}
              </div>
              <ProgressBar position={position} duration={np.duration} />
            </>
          ) : (
            <>
              <div className="np-card__title">Nothing playing</div>
              <div className="muted">Add a song, or set a fallback playlist in Settings.</div>
            </>
          )}
          {(canPause || canSkip) && (
            <>
              <div className="np-card__controls">
                {canPause &&
                  (paused ? (
                    <button
                      className="btn btn--primary"
                      disabled={busy}
                      onClick={() => run(() => api.resume(), "Resumed on every device")}
                      title="Resume playback on every device"
                    >
                      <Play size={18} fill="currentColor" /> Resume
                    </button>
                  ) : (
                    <button
                      className="btn"
                      disabled={busy || !np}
                      onClick={() => run(() => api.pause(), "Paused on every device")}
                      title="Pause playback on every device"
                    >
                      <Pause size={18} fill="currentColor" /> Pause
                    </button>
                  ))}
                {canSkip && (
                  <button className="btn" disabled={busy || !np} onClick={skip} title="Skip this song on every device">
                    <SkipForward size={18} fill="currentColor" /> Skip
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      </section>
      {confirmDialog}

      <div className={cx("queue-grid", !canAdd && "queue-grid--single")}>
        <section>
          <h2 className="section-title">
            Up next {upcoming.length > 0 && <span className="muted">{upcoming.length}</span>}
          </h2>
          {upcoming.length === 0 ? (
            <div className="empty-state">
              <strong>Queue is empty</strong>
              {state?.up_next_fallback && (
                <span>
                  Then from the playlist: {state.up_next_fallback.title} — {state.up_next_fallback.artist}
                </span>
              )}
            </div>
          ) : (
            <DndContext
              sensors={sensors}
              collisionDetection={closestCenter}
              modifiers={[restrictToVerticalAxis, restrictToParentElement]}
              onDragEnd={onDragEnd}
              accessibility={{
                screenReaderInstructions: {
                  draggable: "To reorder, press space or enter to pick up the song, use the arrow keys to move it, then press space or enter again to drop it. Press escape to cancel.",
                },
                announcements: {
                  onDragStart: ({ active }) => `Picked up ${titleOf(upcoming, active.id)}.`,
                  onDragOver: ({ active, over }) =>
                    over ? `${titleOf(upcoming, active.id)} is over position ${posOf(upcoming, over.id)}.` : `${titleOf(upcoming, active.id)} is no longer over the list.`,
                  onDragEnd: ({ active, over }) =>
                    over ? `${titleOf(upcoming, active.id)} dropped at position ${posOf(upcoming, over.id)}.` : `${titleOf(upcoming, active.id)} dropped.`,
                  onDragCancel: ({ active }) => `Reordering cancelled. ${titleOf(upcoming, active.id)} was not moved.`,
                },
              }}
            >
              <SortableContext items={upcoming.map((q) => q.id)} strategy={verticalListSortingStrategy}>
                <ol className="list queue-list">
                  {upcoming.map((q, i) => (
                    <SortableQueueRow key={q.id} item={q} disabled={busy || !canReorder}>
                      {(handle) => (
                        <TrackRow
                          track={q.track}
                          index={i + 1}
                          className="card-row"
                          wrap
                          lead={canReorder ? handle : undefined}
                          meta={q.source === "request" ? <span className="muted">Guest request</span> : undefined}
                          actions={
                            (canReorder || canRemove) && (
                            <div className="btn-group btn-group--icons">
                              {canReorder && (
                              <>
                              <button className="icon-btn" disabled={busy || i === 0} onClick={() => move(q, 0)} aria-label="Move to top" title="Move to top">
                                <ArrowUpToLine size={18} />
                              </button>
                              </>
                              )}
                              {canRemove && (
                              <button
                                className="icon-btn icon-btn--danger"
                                disabled={busy}
                                onClick={() => run(() => api.queueRemove(q.id), `Removed “${q.track.title}”`)}
                                aria-label="Remove from queue"
                                title="Remove"
                              >
                                <Trash2 size={18} />
                              </button>
                              )}
                            </div>
                            )
                          }
                        />
                      )}
                    </SortableQueueRow>
                  ))}
                </ol>
              </SortableContext>
            </DndContext>
          )}
          {upcoming.length > 0 && state?.up_next_fallback && (
            <TrackRow track={state.up_next_fallback} dim meta={<span className="muted">then, from the playlist</span>} />
          )}
        </section>

        {canAdd && (
        <section>
          <h2 className="section-title">Add a song</h2>
          <TrackSearch
            actions={(t) => (
              <div className="btn-group">
                <button className="btn btn--sm" disabled={busy} onClick={() => add(t, "next")} title="Play next">
                  <ListStart size={16} />
                  <span className="hide-sm">Next</span>
                </button>
                <button className="btn btn--primary btn--sm" disabled={busy} onClick={() => add(t, "end")} title="Add to end of queue">
                  <Plus size={16} />
                  <span className="hide-sm">Add</span>
                </button>
              </div>
            )}
          />
        </section>
        )}
      </div>
    </div>
  );
}

const titleOf = (items: QueueItem[], id: string | number) => {
  const q = items.find((u) => u.id === id);
  return q ? `“${q.track.title}”` : "Song";
};
const posOf = (items: QueueItem[], id: string | number) => items.findIndex((u) => u.id === id) + 1;

/** A queue <li> that can be dragged by its grip handle (pointer, touch or keyboard). */
function SortableQueueRow({
  item,
  disabled,
  children,
}: {
  item: QueueItem;
  disabled: boolean;
  children: (handle: ReactNode) => ReactNode;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id: item.id,
    disabled,
  });
  const handle = (
    <button
      type="button"
      ref={setActivatorNodeRef}
      className="icon-btn drag-handle"
      aria-label={`Reorder “${item.track.title}”`}
      title="Drag to reorder"
      disabled={disabled}
      {...attributes}
      {...listeners}
    >
      <GripVertical size={18} />
    </button>
  );
  return (
    <li
      ref={setNodeRef}
      className={cx("queue-item", isDragging && "is-dragging")}
      style={{ transform: CSS.Translate.toString(transform), transition }}
    >
      {children(handle)}
    </li>
  );
}
