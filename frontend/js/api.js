// Thin client for the Cinephile API. Same origin, so paths are relative.

async function request(path, { method = "GET", body, signal } = {}) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  if (!response.ok) {
    const error = new Error(await errorMessage(response));
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

async function errorMessage(response) {
  try {
    const data = await response.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail) && data.detail[0]?.msg) return data.detail[0].msg.replace(/^Value error, /, "");
  } catch { /* not JSON */ }
  return `Request failed (${response.status}).`;
}

const query = (params) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : "";
};

/** Stream a chat turn. Calls onEvent for every server event; resolves when the stream ends. */
export async function chatStream(body, onEvent, signal) {
  const response = await fetch("/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw new Error(await errorMessage(response));

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const line = block.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

const item = (it) => ({
  tmdb_id: it.tmdb_id ?? it.id,
  media_type: it.media_type || "movie",
  title: it.title,
  year: it.year || null,
  poster_path: it.poster_path || null,
});

const idOf = (it) => it.tmdb_id ?? it.id;
/** "/favorites/movie/27205": library items are addressed by type and TMDB id. */
const titlePath = (base, it) => `${base}/${it.media_type || "movie"}/${idOf(it)}`;

export const api = {
  health: () => request("/health"),

  threads: {
    list: (q) => request(`/threads${query({ q })}`),
    get: (id) => request(`/threads/${encodeURIComponent(id)}`),
    rename: (id, title) => request(`/threads/${encodeURIComponent(id)}`, { method: "PATCH", body: { title } }),
    pin: (id, pinned) => request(`/threads/${encodeURIComponent(id)}`, { method: "PATCH", body: { pinned } }),
    remove: (id) => request(`/threads/${encodeURIComponent(id)}`, { method: "DELETE" }),
  },

  search: (q, mediaType, signal) => request(`/search${query({ q, media_type: mediaType })}`, { signal }),
  trending: (mediaType = "all") => request(`/trending${query({ media_type: mediaType })}`),
  discover: (params, signal) => request(`/discover${query(params)}`, { signal }),
  genres: (mediaType) => request(`/genres${query({ media_type: mediaType })}`),
  forYou: () => request("/for-you"),
  title: (mediaType, id) => request(`/titles/${mediaType}/${id}`),
  similar: (mediaType, id) => request(`/titles/${mediaType}/${id}/similar`),

  libraryIds: () => request("/library/ids"),
  favorites: {
    list: () => request("/favorites"),
    add: (it) => request("/favorites", { method: "POST", body: item(it) }),
    remove: (it) => request(titlePath("/favorites", it), { method: "DELETE" }),
  },
  watching: {
    list: () => request("/watching"),
    add: (it, status) => request("/watching", { method: "POST", body: { ...item(it), status } }),
    setStatus: (it, status) => request(titlePath("/watching", it), { method: "PATCH", body: { status } }),
    remove: (it) => request(titlePath("/watching", it), { method: "DELETE" }),
  },
  reviews: {
    list: () => request("/reviews"),
    /** The user's review of one title, or null if there is none. */
    get: (it) => request(titlePath("/reviews", it)).catch((error) => {
      if (error.status === 404) return null;
      throw error;
    }),
    save: (it, rating, comment) => request("/reviews", { method: "POST", body: { ...item(it), rating, comment } }),
    remove: (it) => request(titlePath("/reviews", it), { method: "DELETE" }),
  },
};
