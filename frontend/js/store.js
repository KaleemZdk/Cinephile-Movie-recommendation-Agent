// Shared client state: library membership, threads, and tiny pub/sub.
import { api } from "./api.js";
import { toast } from "./ui.js";

const listeners = new Map();

export function on(event, handler) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(handler);
  return () => listeners.get(event).delete(handler);
}

export function emit(event, data) {
  for (const handler of listeners.get(event) || []) handler(data);
}

export const store = {
  threads: [],
  threadQuery: "",
  activeThreadId: null,
  favorites: new Set(),        // "movie:123"
  watching: new Map(),         // "movie:123" -> "plan" | "watching" | "watched"
  reviews: new Map(),          // "movie:123" -> rating
  streaming: new Set(),        // thread ids with a reply in progress
};

export const keyOf = (item) => `${item.media_type || "movie"}:${item.tmdb_id ?? item.id}`;
export const isFavorite = (item) => store.favorites.has(keyOf(item));
export const watchStatus = (item) => store.watching.get(keyOf(item)) || null;
export const reviewRating = (item) => store.reviews.get(keyOf(item)) ?? null;

export const STATUS_LABELS = { plan: "Plan to watch", watching: "Watching", watched: "Watched" };

// ---------------------------------------------------------------- library

export async function loadLibrary() {
  try {
    const ids = await api.libraryIds();
    store.favorites = new Set(ids.favorites);
    store.watching = new Map(Object.entries(ids.watching));
    store.reviews = new Map(Object.entries(ids.reviews));
    emit("library");
  } catch { /* the UI still works without save markers */ }
}

export async function toggleFavorite(item) {
  const key = keyOf(item);
  const had = store.favorites.has(key);
  if (had) store.favorites.delete(key); else store.favorites.add(key);
  emit("library");
  try {
    if (had) await api.favorites.remove(item); else await api.favorites.add(item);
    toast(had ? `Removed ${item.title} from favorites` : `Saved ${item.title} to favorites`);
  } catch (error) {
    if (had) store.favorites.add(key); else store.favorites.delete(key);
    emit("library");
    toast(error.message, { error: true });
  }
}

/** status: "plan" | "watching" | "watched", or null to take it off the list. */
export async function setWatchStatus(item, status) {
  const key = keyOf(item);
  const previous = store.watching.get(key) || null;
  if (status === previous) return;
  if (status) store.watching.set(key, status); else store.watching.delete(key);
  emit("library");
  try {
    if (!status) await api.watching.remove(item);
    else if (previous) await api.watching.setStatus(item, status);
    else await api.watching.add(item, status);
    toast(status ? `${item.title}: ${STATUS_LABELS[status].toLowerCase()}` : `Removed ${item.title} from your watch list`);
  } catch (error) {
    if (previous) store.watching.set(key, previous); else store.watching.delete(key);
    emit("library");
    toast(error.message, { error: true });
  }
}

export async function saveReview(item, rating, comment) {
  await api.reviews.save(item, rating, comment);
  store.reviews.set(keyOf(item), rating);
  emit("library");
  emit("reviews");
  toast("Review saved");
}

export async function deleteReview(item) {
  await api.reviews.remove(item);
  store.reviews.delete(keyOf(item));
  emit("library");
  emit("reviews");
  toast("Review deleted");
}

// ---------------------------------------------------------------- threads

function sortThreads(threads) {
  return threads.sort((a, b) => (Number(b.pinned) - Number(a.pinned)) || b.updated_at.localeCompare(a.updated_at));
}

export async function loadThreads() {
  try {
    const { threads } = await api.threads.list(store.threadQuery);
    store.threads = threads;
    emit("threads");
  } catch (error) {
    toast(`Could not load chats: ${error.message}`, { error: true });
  }
}

export function upsertThread(thread) {
  if (!thread) return;
  const index = store.threads.findIndex((t) => t.id === thread.id);
  if (index >= 0) store.threads[index] = thread; else store.threads.push(thread);
  sortThreads(store.threads);
  emit("threads");
}

export function dropThread(id) {
  store.threads = store.threads.filter((t) => t.id !== id);
  emit("threads");
}
