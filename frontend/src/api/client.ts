import type { Job, Limits, PaperDetail, PaperPage, Progress, Seed } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Fired when the session has expired, so the app can return to the sign-in page. */
export const UNAUTHORIZED_EVENT = "ls:unauthorized";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  // The server rejects writes without this header (a cross-site form cannot set it).
  headers.set("X-Requested-With", "LitScout");
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (res.status === 401 && !path.startsWith("/api/auth/login")) {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : body.detail?.[0]?.msg ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail || `Request failed (${res.status})`);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const json = (body: unknown) => JSON.stringify(body);

export const api = {
  login: (username: string, password: string) =>
    request<{ username: string }>("/api/auth/login", { method: "POST", body: json({ username, password }) }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  me: () => request<{ username: string }>("/api/auth/me"),
  limits: () => request<Limits>("/api/limits"),
  jobs: () => request<Job[]>("/api/jobs"),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  createJob: (form: FormData) => request<Job>("/api/jobs", { method: "POST", body: form }),
  cancel: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: "POST" }),
  resume: (id: string) => request<Job>(`/api/jobs/${id}/resume`, { method: "POST" }),
  remove: (id: string) => request<void>(`/api/jobs/${id}`, { method: "DELETE" }),
  progress: (id: string) => request<Progress>(`/api/jobs/${id}/progress`),
  criteria: (id: string) => request<{ text: string | null; editable: boolean }>(`/api/jobs/${id}/criteria`),
  saveCriteria: (id: string, text: string) =>
    request<{ text: string | null; editable: boolean }>(`/api/jobs/${id}/criteria`, { method: "PUT", body: json({ text }) }),
  papers: (id: string, params: Record<string, string | number | undefined>) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)));
    return request<PaperPage>(`/api/jobs/${id}/papers?${q}`);
  },
  paper: (id: string, pid: string) => request<PaperDetail>(`/api/jobs/${id}/paper?pid=${encodeURIComponent(pid)}`),
  seeds: (id: string) => request<Seed[]>(`/api/jobs/${id}/seeds`),
  exportUrl: (id: string, fmt: "xlsx" | "csv") => `/api/jobs/${id}/export/${fmt}`,
};
