import type { components } from "./generated/openapi";

export type Schemas = components["schemas"];

export type User = Schemas["UserPublic"];
export type TokenResponse = Schemas["TokenResponse"];
export type FeedItem = Omit<Schemas["FeedItem"], "cluster_id"> & { cluster_id: number | null };
export type FeedResponse = Omit<Schemas["FeedResponse"], "items" | "next_cursor"> & { items: FeedItem[]; next_cursor: string | null };
export type ClusterItem = Schemas["ClusterItemPublic"];
export type StoryCluster = Omit<Schemas["ClusterPublic"], "curation_state"> & { curation_state: "ready" | "partial" | "recalculating" | "empty" };
export type BriefingItem = Schemas["BriefingItemPublic"];
export type Briefing = Omit<Schemas["BriefingPublic"], "status"> & { status: "pending" | "ready" | "failed" | string };
export type BriefingSchedule = Schemas["BriefingSchedulePublic"];
export type TopicPreference = Schemas["TopicPreferencePublic"];
export type SourcePreference = Schemas["SourcePreferencePublic"];
export type PreferenceProfile = Schemas["PreferenceProfilePublic"];
export type RankingPreference = Schemas["RankingPreferencePublic"];
export type UserSettings = Schemas["UserSettings"];
export type UserSettingsUpdate = Schemas["UserSettingsUpdate"];
export type HealthStatus = Schemas["HealthStatusPublic"];
export type FeedbackEvent = Omit<Schemas["FeedbackPublic"], "action_type" | "rating"> & {
  action_type: "article" | "source" | "topic" | "quality";
  rating: "like" | "dislike" | "neutral";
};

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // Keep the status-based message when the server does not return JSON.
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function publicRequest<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new ApiError(response.status, `Request failed (${response.status})`);
  return (await response.json()) as T;
}

export function getHealthStatus(): Promise<HealthStatus> {
  return publicRequest<HealthStatus>("/health/status");
}

export function login(email: string, password: string): Promise<TokenResponse> {
  return request<TokenResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function register(email: string, password: string): Promise<TokenResponse> {
  return request<TokenResponse>("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function refresh(): Promise<TokenResponse> {
  return request<TokenResponse>("/auth/refresh", { method: "POST" });
}

export function logout(): Promise<void> {
  return request<void>("/auth/logout", { method: "POST" });
}

export function getFeed(token: string, tag?: string, cursor?: string): Promise<FeedResponse> {
  const query = new URLSearchParams({ page_size: "20", status: "published" });
  if (tag) query.set("tag", tag);
  if (cursor) query.set("cursor", cursor);
  return request<FeedResponse>(`/feed?${query.toString()}`, {}, token);
}

export function getClusters(token: string): Promise<StoryCluster[]> {
  return request<StoryCluster[]>("/clusters", {}, token);
}

export function getBriefings(token: string): Promise<Briefing[]> {
  return request<Briefing[]>("/briefings", {}, token);
}

export function getBriefingSchedule(token: string): Promise<BriefingSchedule> {
  return request<BriefingSchedule>("/briefings/schedule", {}, token);
}

export function updateBriefingSchedule(token: string, payload: Omit<BriefingSchedule, "id">): Promise<BriefingSchedule> {
  return request<BriefingSchedule>("/briefings/schedule", {
    method: "PUT",
    body: JSON.stringify(payload),
  }, token);
}

export function generateBriefing(token: string, payload: { briefing_date: string; local_time: string; timezone: string; regenerate?: boolean }): Promise<Briefing> {
  return request<Briefing>("/briefings/generate", {
    method: "POST",
    body: JSON.stringify(payload),
  }, token);
}

export function regenerateBriefing(token: string, briefingId: number): Promise<Briefing> {
  return request<Briefing>(`/briefings/${briefingId}/regenerate`, { method: "POST" }, token);
}

export function getPreferences(token: string): Promise<PreferenceProfile> {
  return request<PreferenceProfile>("/preferences", {}, token);
}

export function updateRankingPreference(token: string, serendipity: number): Promise<RankingPreference> {
  return request<RankingPreference>("/preferences/ranking", {
    method: "PATCH",
    body: JSON.stringify({ serendipity }),
  }, token);
}

export function getUserSettings(token: string): Promise<UserSettings> {
  return request<UserSettings>("/preferences/settings", {}, token);
}

export function updateUserSettings(token: string, payload: UserSettingsUpdate): Promise<UserSettings> {
  return request<UserSettings>("/preferences/settings", {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, token);
}

export function getCurrentFeedback(token: string, articleIds: number[]): Promise<FeedbackEvent[]> {
  const query = new URLSearchParams();
  articleIds.forEach((articleId) => query.append("article_id", String(articleId)));
  return request<FeedbackEvent[]>(`/feedback${query.size ? `?${query.toString()}` : ""}`, {}, token);
}

export function submitFeedback(
  token: string,
  payload: {
    action_type: FeedbackEvent["action_type"];
    rating: FeedbackEvent["rating"];
    article_id?: number;
    source_id?: number;
    topic_name?: string;
  },
): Promise<FeedbackEvent> {
  return request<FeedbackEvent>("/feedback", { method: "POST", body: JSON.stringify(payload) }, token);
}

export function undoFeedback(token: string, feedbackId: number): Promise<FeedbackEvent> {
  return request<FeedbackEvent>(`/feedback/${feedbackId}/undo`, { method: "POST" }, token);
}

export function updateTopicPreference(token: string, topicName: string, weightScore: number): Promise<TopicPreference> {
  return request<TopicPreference>(`/preferences/topics/${encodeURIComponent(topicName)}`, {
    method: "PATCH",
    body: JSON.stringify({ weight_score: weightScore }),
  }, token);
}

export function updateSourcePreference(token: string, sourceId: number, isMuted: boolean): Promise<SourcePreference> {
  return request<SourcePreference>(`/preferences/sources/${sourceId}`, {
    method: "PATCH",
    body: JSON.stringify({ is_muted: isMuted }),
  }, token);
}

export function resetTopicPreference(token: string, topicName: string): Promise<void> {
  return request<void>(`/preferences/topics/${encodeURIComponent(topicName)}/reset`, {
    method: "POST",
    body: JSON.stringify({ confirm: true }),
  }, token);
}

export function resetSourcePreference(token: string, sourceId: number): Promise<void> {
  return request<void>(`/preferences/sources/${sourceId}/reset`, {
    method: "POST",
    body: JSON.stringify({ confirm: true }),
  }, token);
}

export type SourceRecord = Schemas["SourcePublic"];
export type UsageReport = Schemas["UsageReportPublic"];
export type AlertsReport = Schemas["AlertsPublic"];
export type AdminUser = Schemas["UserAdminPublic"];
export type AuditEventRow = Schemas["AuditEventPublic"];
export type InvitationCreated = Schemas["InvitationCreated"];

export function getSources(token: string): Promise<SourceRecord[]> {
  return request<SourceRecord[]>("/sources", {}, token);
}

export function createSource(token: string, payload: Schemas["SourceCreate"]): Promise<SourceRecord> {
  return request<SourceRecord>("/sources", { method: "POST", body: JSON.stringify(payload) }, token);
}

export function updateSource(token: string, sourceId: number, payload: Partial<Schemas["SourceUpdate"]>): Promise<SourceRecord> {
  return request<SourceRecord>(`/sources/${sourceId}`, { method: "PATCH", body: JSON.stringify(payload) }, token);
}

export function deleteSource(token: string, sourceId: number): Promise<void> {
  return request<void>(`/sources/${sourceId}`, { method: "DELETE" }, token);
}

export function ingestSource(token: string, sourceId: number): Promise<{ run_id: number; status: string }> {
  return request<{ run_id: number; status: string }>(`/sources/${sourceId}/ingest`, { method: "POST" }, token);
}

export function devPublishExtracted(token: string): Promise<{ published: number }> {
  return request<{ published: number }>("/admin/dev/publish-extracted", { method: "POST" }, token);
}

export function getAdminUsers(token: string): Promise<AdminUser[]> {
  return request<AdminUser[]>("/admin/users", {}, token);
}

export function createInvitation(token: string, payload: Schemas["InvitationCreateRequest"]): Promise<InvitationCreated> {
  return request<InvitationCreated>("/admin/invitations", { method: "POST", body: JSON.stringify(payload) }, token);
}

export function getUsageReport(token: string, from: string, to: string): Promise<UsageReport> {
  return request<UsageReport>(`/admin/usage-events/report?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, {}, token);
}

export function getAuditEvents(token: string, limit = 25): Promise<Schemas["AuditEventExport"]> {
  return request<Schemas["AuditEventExport"]>(`/admin/audit-events?limit=${limit}`, {}, token);
}

export function getAlerts(token: string): Promise<AlertsReport> {
  return request<AlertsReport>("/admin/alerts", {}, token);
}
