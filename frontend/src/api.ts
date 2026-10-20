import type { Bookmark, BookmarkInput } from "./types";

let token: string | null = null;

export class ApiError extends Error {
  constructor(public readonly status: number) {
    super(`API 요청 실패 (${status})`);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`/api${path}`, { ...init, headers });
  if (!response.ok) throw new ApiError(response.status);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function login(username: string, password: string): Promise<void> {
  const body = await request<{ token: string }>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  token = body.token;
}

export async function logout(): Promise<void> {
  try {
    await request<void>("/auth/logout", { method: "POST" });
  } finally {
    token = null;
  }
}

export function listBookmarks(filter: { q: string; tag: string }): Promise<Bookmark[]> {
  const params = new URLSearchParams();
  if (filter.q) params.set("q", filter.q);
  if (filter.tag) params.set("tag", filter.tag);
  const query = params.toString();
  return request<Bookmark[]>(`/bookmarks${query ? `?${query}` : ""}`);
}

export function listTags(): Promise<string[]> {
  return request<string[]>("/tags");
}

export function createBookmark(input: BookmarkInput): Promise<Bookmark> {
  return request<Bookmark>("/bookmarks", { method: "POST", body: JSON.stringify(input) });
}

export function updateBookmark(id: number, input: BookmarkInput): Promise<Bookmark> {
  return request<Bookmark>(`/bookmarks/${id}`, { method: "PATCH", body: JSON.stringify(input) });
}

export function deleteBookmark(id: number): Promise<void> {
  return request<void>(`/bookmarks/${id}`, { method: "DELETE" });
}
