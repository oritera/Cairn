const BASE_URL = import.meta.env.VITE_API_URL || "";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem("token");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers });
  if (res.status === 401) {
    localStorage.removeItem("token");
    localStorage.removeItem("username");
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `request failed: ${res.status}`);
  }
  return res.json();
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  username: string;
}

export interface UserInfo {
  id: string;
  username: string;
  created_at: string;
}

export interface ProjectSummary {
  id: string;
  title: string;
  status: "active" | "stopped" | "completed";
  bootstrap_enabled: boolean;
  created_at: string;
  fact_count: number;
  intent_count: number;
  working_intent_count: number;
  unclaimed_intent_count: number;
  hint_count: number;
}

export const api = {
  register: (username: string, password: string) =>
    request<TokenResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  login: (username: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  me: () => request<UserInfo>("/auth/me"),

  listProjects: () => request<ProjectSummary[]>("/projects"),
};
