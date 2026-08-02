import { FormEvent, useDeferredValue, useEffect, useRef, useState } from "react";
import {
  AdminUser,
  AlertsReport,
  ApiError,
  Briefing,
  BriefingSchedule,
  FeedbackEvent,
  FeedItem,
  HealthStatus,
  PreferenceProfile,
  SourceRecord,
  SourcePreference,
  StoryCluster,
  TokenResponse,
  TopicPreference,
  UsageReport,
  User,
  UserSettings,
  UserSettingsUpdate,
  createInvitation,
  createSource,
  deleteSource,
  devPublishExtracted,
  getAdminUsers,
  getAlerts,
  getAuditEvents,
  getBriefingSchedule,
  getBriefings,
  getClusters,
  getCurrentFeedback,
  getFeed,
  getHealthStatus,
  getPreferences,
  getSources,
  getUsageReport,
  getUserSettings,
  generateBriefing,
  ingestSource,
  login,
  logout,
  refresh,
  register,
  regenerateBriefing,
  resetSourcePreference,
  resetTopicPreference,
  submitFeedback,
  undoFeedback,
  updateRankingPreference,
  updateBriefingSchedule,
  updateSource,
  updateSourcePreference,
  updateTopicPreference,
  updateUserSettings,
  type Schemas,
} from "./api";

type Theme = "dark" | "light";
type AuthMode = "login" | "register";
type FeedMode = "grouped" | "flat" | "timeline";
type DashboardView = "feed" | "briefings" | "profile" | "sources" | "admin";

const SESSION_KEY = "tuxnews.session";
const THEME_KEY = "tuxnews.theme";

type Session = {
  accessToken: string;
  user: User;
};

function readSession(): Session | null {
  try {
    const value = sessionStorage.getItem(SESSION_KEY);
    return value ? (JSON.parse(value) as Session) : null;
  } catch {
    return null;
  }
}

function readTheme(): Theme {
  return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
}

function localDateForTimezone(timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function saveSession(response: TokenResponse): Session {
  const session = { accessToken: response.access_token, user: response.user };
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  return session;
}

function friendlyError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Your session expired. Sign in again to continue.";
    return error.message;
  }
  return "The service is unavailable right now. Try again in a moment.";
}

export default function App() {
  const [session, setSession] = useState<Session | null>(readSession);
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [booting, setBooting] = useState(Boolean(session));

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  useEffect(() => {
    if (!session) {
      setBooting(false);
      return;
    }
    let active = true;
    void fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      credentials: "include",
    }).then(async (response) => {
      if (response.ok || !active) return;
      try {
        const renewed = saveSession(await refresh());
        if (active) setSession(renewed);
      } catch {
        sessionStorage.removeItem(SESSION_KEY);
        if (active) setSession(null);
      }
    }).catch(() => {
      if (active) setSession(null);
    }).finally(() => {
      if (active) setBooting(false);
    });
    return () => {
      active = false;
    };
  }, []);

  const handleAuthenticated = (response: TokenResponse) => setSession(saveSession(response));
  const handleLogout = async () => {
    try {
      await logout();
    } finally {
      sessionStorage.removeItem(SESSION_KEY);
      setSession(null);
    }
  };

  if (booting) return <div className="screen-state"><span className="spinner" />Checking your secure session…</div>;
  if (!session) return <AuthPanel onAuthenticated={handleAuthenticated} />;
  return <Dashboard session={session} theme={theme} onThemeChange={setTheme} onLogout={handleLogout} />;
}

function AuthPanel({ onAuthenticated }: { onAuthenticated: (response: TokenResponse) => void }) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const response = mode === "login" ? await login(email, password) : await register(email, password);
      onAuthenticated(response);
    } catch (requestError) {
      setError(friendlyError(requestError));
    } finally {
      setPending(false);
    }
  };

  return (
    <main className="auth-layout">
      <section className="auth-intro">
        <p className="eyebrow">PERSONAL INTELLIGENCE DESK / 01</p>
        <h1>Read with a little more signal.</h1>
        <p className="hero-copy">A private reading desk for the stories that deserve your attention, not another feed shouting for it.</p>
        <div className="principles" aria-label="Product principles">
          <span>PRIVATE BY DEFAULT</span>
          <span>EXPLAINABLE RANKING</span>
          <span>LOCAL ARCHIVE</span>
        </div>
      </section>
      <section className="auth-card" aria-labelledby="auth-heading">
        <div className="auth-tabs" role="tablist" aria-label="Account access">
          <button className={mode === "login" ? "tab active" : "tab"} onClick={() => setMode("login")} role="tab" aria-selected={mode === "login"}>Sign in</button>
          <button className={mode === "register" ? "tab active" : "tab"} onClick={() => setMode("register")} role="tab" aria-selected={mode === "register"}>Create account</button>
        </div>
        <p className="eyebrow">WELCOME BACK</p>
        <h2 id="auth-heading">{mode === "login" ? "Open your desk." : "Make it yours."}</h2>
        <p className="muted">Your sources, feedback and archive stay attached to your account.</p>
        <form onSubmit={submit} className="auth-form">
          <label>Email<input type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></label>
          <label>Password<input type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={12} required value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary-button" disabled={pending}>{pending ? "Opening…" : mode === "login" ? "Enter desk" : "Create desk"}</button>
        </form>
      </section>
    </main>
  );
}

function Dashboard({
  session,
  theme,
  onThemeChange,
  onLogout,
}: {
  session: Session;
  theme: Theme;
  onThemeChange: (theme: Theme) => void;
  onLogout: () => Promise<void>;
}) {
  const [view, setView] = useState<DashboardView>("feed");
  const [feedMode, setFeedMode] = useState<FeedMode>("flat");
  const [tag, setTag] = useState("");
  const deferredTag = useDeferredValue(tag.trim().toLowerCase());
  const [items, setItems] = useState<FeedItem[]>([]);
  const [feedbackByArticle, setFeedbackByArticle] = useState<Record<number, FeedbackEvent>>({});
  const [feedbackByQuality, setFeedbackByQuality] = useState<Record<number, FeedbackEvent>>({});
  const [feedbackBySource, setFeedbackBySource] = useState<Record<number, FeedbackEvent>>({});
  const [feedbackByTopic, setFeedbackByTopic] = useState<Record<string, FeedbackEvent>>({});
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedAttempt, setFeedAttempt] = useState(0);
  const [clusters, setClusters] = useState<StoryCluster[]>([]);
  const [clustersLoading, setClustersLoading] = useState(true);
  const [clustersError, setClustersError] = useState<string | null>(null);
  const [selectedClusterId, setSelectedClusterId] = useState<number | null>(null);
  const [profile, setProfile] = useState<PreferenceProfile | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileAttempt, setProfileAttempt] = useState(0);
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [briefings, setBriefings] = useState<Briefing[]>([]);
  const [briefingSchedule, setBriefingSchedule] = useState<BriefingSchedule | null>(null);
  const [briefingLoading, setBriefingLoading] = useState(false);
  const [briefingError, setBriefingError] = useState<string | null>(null);
  const [briefingAttempt, setBriefingAttempt] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    void getFeed(session.accessToken, deferredTag || undefined).then((response) => {
      if (!active) return;
      setItems(response.items);
      setFeedbackByArticle({});
      setFeedbackByQuality({});
      setFeedbackBySource({});
      setFeedbackByTopic({});
      setNextCursor(response.next_cursor);
    }).catch((requestError) => {
      if (active) setError(friendlyError(requestError));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [session.accessToken, deferredTag, feedAttempt]);

  useEffect(() => {
    let active = true;
    setClustersLoading(true);
    setClustersError(null);
    void getClusters(session.accessToken).then((response) => {
      if (!active) return;
      setClusters(response);
      setSelectedClusterId((current) => current !== null && response.some((cluster) => cluster.id === current) ? current : null);
    }).catch((requestError) => {
      if (active) setClustersError(friendlyError(requestError));
    }).finally(() => {
      if (active) setClustersLoading(false);
    });
    return () => {
      active = false;
    };
  }, [session.accessToken, feedAttempt]);

  useEffect(() => {
    if (!items.length) return;
    let active = true;
    void getCurrentFeedback(session.accessToken, []).then((events) => {
      if (!active) return;
      const article: Record<number, FeedbackEvent> = {};
      const quality: Record<number, FeedbackEvent> = {};
      const source: Record<number, FeedbackEvent> = {};
      const topic: Record<string, FeedbackEvent> = {};
      events.forEach((event) => {
        if (event.action_type === "article" && event.article_id !== null) article[event.article_id] = event;
        else if (event.action_type === "quality" && event.article_id !== null) quality[event.article_id] = event;
        else if (event.action_type === "source" && event.source_id !== null) source[event.source_id] = event;
        else if (event.action_type === "topic" && event.topic_name !== null) topic[event.topic_name] = event;
      });
      setFeedbackByArticle(article);
      setFeedbackByQuality(quality);
      setFeedbackBySource(source);
      setFeedbackByTopic(topic);
    }).catch(() => {
      // Feedback controls still work when a profile read is temporarily unavailable.
    });
    return () => {
      active = false;
    };
  }, [items, session.accessToken]);

  useEffect(() => {
    if (view !== "profile") return;
    let active = true;
    setProfileError(null);
    setSettingsError(null);
    void Promise.all([getPreferences(session.accessToken), getUserSettings(session.accessToken)]).then(([response, userSettings]) => {
      if (!active) return;
      setProfile(response);
      setSettings(userSettings);
    }).catch((requestError) => {
      if (active) {
        const message = friendlyError(requestError);
        setProfileError(message);
        setSettingsError(message);
      }
    });
    return () => {
      active = false;
    };
  }, [session.accessToken, profileAttempt, view]);

  useEffect(() => {
    if (view !== "briefings") return;
    let active = true;
    setBriefingLoading(true);
    setBriefingError(null);
    void Promise.all([getBriefings(session.accessToken), getBriefingSchedule(session.accessToken)]).then(([editionResponse, scheduleResponse]) => {
      if (!active) return;
      setBriefings(editionResponse);
      setBriefingSchedule(scheduleResponse);
    }).catch((requestError) => {
      if (active) setBriefingError(friendlyError(requestError));
    }).finally(() => {
      if (active) setBriefingLoading(false);
    });
    return () => {
      active = false;
    };
  }, [session.accessToken, briefingAttempt, view]);

  useEffect(() => {
    let active = true;
    const refreshHealth = async () => {
      try {
        const response = await getHealthStatus();
        if (active) setHealth(response);
      } catch {
        if (active) setHealth(null);
      }
    };
    void refreshHealth();
    const interval = window.setInterval(() => void refreshHealth(), 30_000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  const announce = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(null), 3600);
  };

  const setFeedbackFor = (
    actionType: FeedbackEvent["action_type"],
    key: number | string,
    event: FeedbackEvent | undefined,
  ) => {
    if (actionType === "article") {
      setFeedbackByArticle((current) => { const next = { ...current }; if (event) next[key as number] = event; else delete next[key as number]; return next; });
    } else if (actionType === "quality") {
      setFeedbackByQuality((current) => { const next = { ...current }; if (event) next[key as number] = event; else delete next[key as number]; return next; });
    } else if (actionType === "source") {
      setFeedbackBySource((current) => { const next = { ...current }; if (event) next[key as number] = event; else delete next[key as number]; return next; });
    } else {
      setFeedbackByTopic((current) => { const next = { ...current }; if (event) next[key as string] = event; else delete next[key as string]; return next; });
    }
  };

  const reorderAfterArticleFeedback = (itemId: number, delta: number) => {
    setItems((current) => {
      const next = current.map((entry) => entry.id === itemId
        ? { ...entry, relevance_score: Math.max(0, Math.min(1, (entry.relevance_score ?? 0) + delta)) }
        : entry);
      return [...next].sort(
        (a, b) => (b.relevance_score ?? 0) - (a.relevance_score ?? 0)
          || Date.parse(b.published_at ?? "") - Date.parse(a.published_at ?? "")
          || b.id - a.id,
      );
    });
  };

  const handleFeedback = async (
    item: FeedItem,
    actionType: FeedbackEvent["action_type"],
    rating: Exclude<FeedbackEvent["rating"], "neutral">,
  ) => {
    const topicKey = item.tags[0] ?? null;
    const previous = actionType === "article" ? feedbackByArticle[item.id]
      : actionType === "quality" ? feedbackByQuality[item.id]
      : actionType === "source" ? feedbackBySource[item.source_id]
      : topicKey !== null ? feedbackByTopic[topicKey] : undefined;
    const feedbackKey = actionType === "source" ? item.source_id : actionType === "topic" ? topicKey : item.id;
    const undoing = previous?.action_type === actionType && previous.rating === rating && previous.id > 0;
    const optimistic: FeedbackEvent = {
      id: previous?.id ?? -Date.now(),
      user_id: session.user.id,
      action_type: actionType,
      rating,
      article_id: actionType === "source" || actionType === "topic" ? null : item.id,
      source_id: actionType === "source" ? item.source_id : null,
      topic_name: actionType === "topic" ? topicKey : null,
      reason: null,
      supersedes_id: previous?.id ?? null,
      is_current: true,
    };
    if (feedbackKey !== null) setFeedbackFor(actionType, feedbackKey, optimistic);
    try {
      const result = undoing
        ? await undoFeedback(session.accessToken, previous.id)
        : await submitFeedback(session.accessToken, {
            action_type: actionType,
            rating,
            article_id: actionType === "article" || actionType === "quality" ? item.id : undefined,
            source_id: actionType === "source" ? item.source_id : undefined,
            topic_name: actionType === "topic" ? topicKey ?? undefined : undefined,
          });
      if (feedbackKey !== null) {
        if (result.rating === "neutral") setFeedbackFor(actionType, feedbackKey, undefined);
        else setFeedbackFor(actionType, feedbackKey, result);
      }
      if (actionType === "article") {
        const delta = undoing
          ? (previous.rating === "like" ? -0.15 : previous.rating === "dislike" ? 0.15 : 0)
          : rating === "like" ? 0.15 : -0.15;
        if (delta !== 0) reorderAfterArticleFeedback(item.id, delta);
      }
      announce(undoing ? "Feedback removed. Your ranking will adjust." : `${actionType} feedback saved.`);
    } catch (requestError) {
      if (feedbackKey !== null) {
        if (previous) setFeedbackFor(actionType, feedbackKey, previous);
        else setFeedbackFor(actionType, feedbackKey, undefined);
      }
      announce(friendlyError(requestError));
    }
  };

  const handleTopicUpdate = async (topicName: string, weightScore: number) => {
    try {
      await updateTopicPreference(session.accessToken, topicName, weightScore);
      setProfileAttempt((attempt) => attempt + 1);
      announce("Topic preference updated.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const handleTopicReset = async (topicName: string) => {
    try {
      await resetTopicPreference(session.accessToken, topicName);
      setProfileAttempt((attempt) => attempt + 1);
      announce("Topic preference reset. History was kept.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const handleRankingUpdate = async (serendipity: number) => {
    try {
      await updateRankingPreference(session.accessToken, serendipity);
      setProfileAttempt((attempt) => attempt + 1);
      setFeedAttempt((attempt) => attempt + 1);
      announce("Display mix updated. Your learned signal stayed untouched.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const handleSettingsUpdate = async (payload: UserSettingsUpdate) => {
    try {
      const updated = await updateUserSettings(session.accessToken, payload);
      setSettings(updated);
      announce("Personal settings updated.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const handleSourceMute = async (sourceId: number, isMuted: boolean) => {
    try {
      await updateSourcePreference(session.accessToken, sourceId, isMuted);
      setProfileAttempt((attempt) => attempt + 1);
      announce(isMuted ? "Source muted from your desk." : "Source restored to your desk.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const handleSourceReset = async (sourceId: number) => {
    try {
      await resetSourcePreference(session.accessToken, sourceId);
      setProfileAttempt((attempt) => attempt + 1);
      announce("Source preference reset. Feedback history was kept.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const handleBriefingScheduleUpdate = async (payload: Omit<BriefingSchedule, "id">) => {
    try {
      const updated = await updateBriefingSchedule(session.accessToken, payload);
      setBriefingSchedule(updated);
      announce("Briefing schedule updated.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const handleGenerateBriefing = async () => {
    if (!briefingSchedule) return;
    try {
      await generateBriefing(session.accessToken, {
        briefing_date: localDateForTimezone(briefingSchedule.timezone),
        local_time: briefingSchedule.local_time,
        timezone: briefingSchedule.timezone,
      });
      setBriefingAttempt((attempt) => attempt + 1);
      announce("Briefing generated.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const handleRegenerateBriefing = async (briefingId: number) => {
    try {
      await regenerateBriefing(session.accessToken, briefingId);
      setBriefingAttempt((attempt) => attempt + 1);
      announce("Briefing regenerated.");
    } catch (requestError) {
      announce(friendlyError(requestError));
    }
  };

  const loadMore = async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const response = await getFeed(session.accessToken, deferredTag || undefined, nextCursor);
      setItems((current) => [...current, ...response.items]);
      setNextCursor(response.next_cursor);
    } catch (requestError) {
      setError(friendlyError(requestError));
    } finally {
      setLoadingMore(false);
    }
  };

  const openCluster = (clusterId: number) => {
    setSelectedClusterId(clusterId);
    setFeedMode("timeline");
  };

  const changeFeedMode = (mode: FeedMode) => {
    if (mode === "timeline" && selectedClusterId === null && clusters[0]) {
      setSelectedClusterId(clusters[0].id);
    }
    setFeedMode(mode);
  };

  const selectedCluster = clusters.find((cluster) => cluster.id === selectedClusterId) ?? null;

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <button className="wordmark" onClick={() => setView("feed")} aria-label="Go to reading desk">Tuxnews<span>/</span></button>
        <nav className="main-nav" aria-label="Main navigation">
          <button className={view === "feed" ? "nav-link active" : "nav-link"} onClick={() => setView("feed")}>Reading desk</button>
          <button className={view === "briefings" ? "nav-link active" : "nav-link"} onClick={() => setView("briefings")}>Briefings</button><button className={view === "profile" ? "nav-link active" : "nav-link"} onClick={() => setView("profile")}>Your signal</button>
          <button className={view === "sources" ? "nav-link active" : "nav-link"} onClick={() => setView("sources")}>Sources</button>
          {session.user.role === "admin" && <button className={view === "admin" ? "nav-link active" : "nav-link"} onClick={() => setView("admin")}>Operations</button>}
        </nav>
        <div className="topbar-actions">
          <HealthPanel health={health} />
          <button className="icon-button" onClick={() => onThemeChange(theme === "dark" ? "light" : "dark")} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>{theme === "dark" ? "☼" : "◐"}</button>
          <button className="user-chip" onClick={() => setView("profile")}><span className="avatar">{session.user.email[0].toUpperCase()}</span><span className="user-email">{session.user.email}</span></button>
          <button className="logout-button" onClick={() => void onLogout()}>Exit</button>
        </div>
      </header>
      {view === "feed" ? (
        <section className="feed-view" aria-labelledby="feed-heading">          <div className="feed-heading-row">
            <div>
              <p className="eyebrow">PRIVATE SIGNAL / TODAY</p>
              <h1 id="feed-heading">The reading desk.</h1>
              <p className="lede">Ranked for you. Kept legible. No noise tax.</p>
            </div>
             <div className="feed-tools"><label className="search-field">Filter by tag<input value={tag} onChange={(event) => setTag(event.target.value)} placeholder="linux, design…" /></label><FeedModeSwitch mode={feedMode} onChange={changeFeedMode} /></div>
           </div>
           {loading && <FeedLoading />}
           {!loading && error && <ErrorState message={error} onRetry={() => setFeedAttempt((attempt) => attempt + 1)} />}
           {!loading && !error && feedMode === "flat" && items.length === 0 && <EmptyFeed hasFilter={Boolean(deferredTag)} />}
           {!loading && !error && feedMode === "flat" && items.length > 0 && (
             <>
               <div className="feed-grid">{items.map((item, index) => <ArticleCard item={item} index={index} feedback={feedbackByArticle[item.id]} qualityFeedback={feedbackByQuality[item.id]} sourceFeedback={feedbackBySource[item.source_id]} topicFeedback={feedbackByTopic[item.tags[0] ?? ""]} clusters={clusters} onOpenCluster={openCluster} onFeedback={handleFeedback} key={item.id} />)}</div>
               {nextCursor && <button className="load-more" onClick={() => void loadMore()} disabled={loadingMore}>{loadingMore ? "Loading more…" : "Load more stories"}</button>}
             </>
           )}
           {!loading && !error && feedMode !== "flat" && clustersLoading && <FeedLoading />}
           {!loading && !error && feedMode !== "flat" && !clustersLoading && clustersError && <ErrorState message={clustersError} onRetry={() => setFeedAttempt((attempt) => attempt + 1)} />}
           {!loading && !error && feedMode === "grouped" && !clustersLoading && !clustersError && clusters.length === 0 && <EmptyClusters />}
           {!loading && !error && feedMode === "grouped" && !clustersLoading && !clustersError && clusters.length > 0 && <ClusterGrid clusters={clusters} onOpen={openCluster} />}
           {!loading && !error && feedMode === "timeline" && !clustersLoading && !clustersError && (selectedCluster ? <ClusterTimeline cluster={selectedCluster} onBack={() => setFeedMode("grouped")} /> : <EmptyClusters />)}
        </section>
      ) : view === "briefings" ? (
        <BriefingsView briefings={briefings} schedule={briefingSchedule} loading={briefingLoading} error={briefingError} onRetry={() => setBriefingAttempt((attempt) => attempt + 1)} onScheduleUpdate={handleBriefingScheduleUpdate} onGenerate={handleGenerateBriefing} onRegenerate={handleRegenerateBriefing} />
      ) : view === "sources" ? (
        <SourcesView token={session.accessToken} />
      ) : view === "admin" ? (
        <AdminView token={session.accessToken} />
      ) : (
        <ProfileView
          profile={profile}
          error={profileError}
          settings={settings}
          settingsError={settingsError}
          onRankingUpdate={handleRankingUpdate}
          onSettingsUpdate={handleSettingsUpdate}
          onTopicUpdate={handleTopicUpdate}
          onTopicReset={handleTopicReset}
          onSourceMute={handleSourceMute}
          onSourceReset={handleSourceReset}
        />
      )}
      {notice && <div className="toast" role="status">{notice}</div>}
    </main>
  );
}

function HealthPanel({ health }: { health: HealthStatus | null }) {
  const status = health?.status ?? "unavailable";
  const label = status === "healthy" ? "All systems" : status === "degraded" ? "Partial service" : "Unavailable";
  return (
    <details className="health-menu">
      <summary className={`health-summary ${status}`}><span className="health-dot" />{label}</summary>
      <div className="health-popover">
        <div className="health-popover-heading"><span className="panel-label">SYSTEM STATUS</span><strong>{health?.readiness === "ready" ? "Ready for work" : "Not ready"}</strong></div>
        {health ? (
          <ul className="health-checks">
            {Object.entries(health.checks).map(([name, check]) => (
              <li key={name}><span>{name}</span><strong className={check.status}>{check.status}</strong></li>
            ))}
          </ul>
        ) : <p className="muted">The service status endpoint could not be reached.</p>}
      </div>
    </details>
  );
}

function ArticleCard({
  item,
  index,
  feedback,
  qualityFeedback,
  sourceFeedback,
  topicFeedback,
  clusters,
  onOpenCluster,
  onFeedback,
}: {
  item: FeedItem;
  index: number;
  feedback?: FeedbackEvent;
  qualityFeedback?: FeedbackEvent;
  sourceFeedback?: FeedbackEvent;
  topicFeedback?: FeedbackEvent;
  clusters: StoryCluster[];
  onOpenCluster: (clusterId: number) => void;
  onFeedback: (item: FeedItem, actionType: FeedbackEvent["action_type"], rating: Exclude<FeedbackEvent["rating"], "neutral">) => Promise<void>;
}) {
  const longPressTimer = useRef<number | null>(null);
  const published = item.published_at ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(item.published_at)) : "Unscheduled";
  const score = Math.round(item.relevance_score * 100);
  const cluster = item.cluster_id === null ? null : clusters.find((candidate) => candidate.id === item.cluster_id) ?? null;
  const clearLongPress = () => {
    if (longPressTimer.current !== null) window.clearTimeout(longPressTimer.current);
    longPressTimer.current = null;
  };
  const openAdvanced = (target: EventTarget & HTMLElement) => {
    if (target.parentElement instanceof HTMLDetailsElement) target.parentElement.open = true;
  };
  return (
    <article className="article-card">
      <div className="card-meta"><span className="card-index">{String(index + 1).padStart(2, "0")}</span><span>{published}</span><span>{item.read_time_minutes ?? 1} min read</span></div>
      <div className="card-body">
        <div className="card-kicker-row"><p className="card-kicker">{item.source_name} / {item.tags[0] ?? "UNFILED"}</p>{item.cluster_id !== null && <button className="cluster-badge" onClick={() => onOpenCluster(item.cluster_id as number)} aria-label={`Open story ${cluster?.title ?? item.cluster_id}`}>STORY / {cluster?.item_count ?? "?"}</button>}</div>
        <h2>{item.title}</h2>
        {item.original_title !== item.title && <p className="original-title">Original: {item.original_title}</p>}
        <p className="summary">{item.summary || "No summary available for this story yet."}</p>
      </div>
      <div className="card-footer">
        <div className="score"><span>RELEVANCE</span><strong>{score}</strong><span className="score-track"><span style={{ width: `${score}%` }} /></span></div>
        <div className="card-actions">
          <div className="quick-feedback" aria-label={`Rate ${item.title}`}>
            <button className={feedback?.rating === "like" ? "feedback-button active" : "feedback-button"} onClick={() => void onFeedback(item, "article", "like")} aria-pressed={feedback?.rating === "like"}>Like</button>
            <button className={feedback?.rating === "dislike" ? "feedback-button active negative" : "feedback-button"} onClick={() => void onFeedback(item, "article", "dislike")} aria-pressed={feedback?.rating === "dislike"}>Skip</button>
          </div>
          <details className="advanced-menu">
            <summary
              onPointerDown={(event) => {
                clearLongPress();
                longPressTimer.current = window.setTimeout(() => openAdvanced(event.currentTarget), 550);
              }}
              onPointerUp={clearLongPress}
              onPointerLeave={clearLongPress}
              onPointerCancel={clearLongPress}
              onContextMenu={(event) => {
                event.preventDefault();
                clearLongPress();
                openAdvanced(event.currentTarget);
              }}
            >More</summary>
            <div className="advanced-popover">
              <span>Adjust one signal</span>
              <button className={sourceFeedback?.rating === "like" ? "active" : ""} onClick={() => void onFeedback(item, "source", "like")} aria-pressed={sourceFeedback?.rating === "like"}>{sourceFeedback?.rating === "like" ? "Undo prefer source" : "Prefer source"}</button>
              <button className={sourceFeedback?.rating === "dislike" ? "active" : ""} onClick={() => void onFeedback(item, "source", "dislike")} aria-pressed={sourceFeedback?.rating === "dislike"}>{sourceFeedback?.rating === "dislike" ? "Undo mute source signal" : "Mute source signal"}</button>
              <button className={topicFeedback?.rating === "like" ? "active" : ""} onClick={() => void onFeedback(item, "topic", "like")} disabled={!item.tags[0]} aria-pressed={topicFeedback?.rating === "like"}>{topicFeedback?.rating === "like" ? "Undo prefer topic" : "Prefer topic"}</button>
              <button className={qualityFeedback?.rating === "dislike" ? "active" : ""} onClick={() => void onFeedback(item, "quality", "dislike")} aria-pressed={qualityFeedback?.rating === "dislike"}>{qualityFeedback?.rating === "dislike" ? "Undo lower quality" : "Lower quality"}</button>
            </div>
          </details>
          <a className="read-link" href={item.url} target="_blank" rel="noreferrer">Read <span aria-hidden="true">↗</span></a>
        </div>
      </div>
    </article>
  );
}

function FeedModeSwitch({ mode, onChange }: { mode: FeedMode; onChange: (mode: FeedMode) => void }) {
  return <div className="view-switch" role="tablist" aria-label="Reading desk view"><button className={mode === "grouped" ? "view-tab active" : "view-tab"} onClick={() => onChange("grouped")} role="tab" aria-selected={mode === "grouped"}>Grouped</button><button className={mode === "flat" ? "view-tab active" : "view-tab"} onClick={() => onChange("flat")} role="tab" aria-selected={mode === "flat"}>Flat</button><button className={mode === "timeline" ? "view-tab active" : "view-tab"} onClick={() => onChange("timeline")} role="tab" aria-selected={mode === "timeline"}>Timeline</button></div>;
}

function ClusterGrid({ clusters, onOpen }: { clusters: StoryCluster[]; onOpen: (clusterId: number) => void }) {
  return <div className="cluster-grid" aria-label="Story clusters">{clusters.map((cluster, index) => <ClusterCard key={cluster.id} cluster={cluster} index={index} onOpen={onOpen} />)}</div>;
}

function ClusterCard({ cluster, index, onOpen }: { cluster: StoryCluster; index: number; onOpen: (clusterId: number) => void }) {
  return <article className="cluster-card"><div className="cluster-meta"><span className="card-index">{String(index + 1).padStart(2, "0")}</span><span>{cluster.item_count} {cluster.item_count === 1 ? "article" : "articles"}</span><span>{cluster.source_count} {cluster.source_count === 1 ? "source" : "sources"}</span><ClusterStateLabel state={cluster.curation_state} /></div><div className="cluster-card-heading"><h2>{cluster.title}</h2><button className="secondary-button" onClick={() => onOpen(cluster.id)}>Open timeline</button></div><p className="summary">{cluster.summary || "This story is still being shaped from connected reports."}</p><details className="cluster-details"><summary>Show connected reports</summary><ol>{cluster.items.map((item) => <li key={item.article_id}><div><strong>{item.title}</strong><span>{item.source_name} / {formatStoryDate(item.published_at)}</span></div><a href={item.url} target="_blank" rel="noreferrer" aria-label={`Read ${item.title}`}>Read ↗</a></li>)}</ol></details></article>;
}

function ClusterTimeline({ cluster, onBack }: { cluster: StoryCluster; onBack: () => void }) {
  return <section className="timeline-panel" aria-labelledby="timeline-heading"><div className="timeline-heading-row"><div><p className="eyebrow">STORY TIMELINE / {cluster.algorithm_version}</p><h2 id="timeline-heading">{cluster.title}</h2><p className="lede">{cluster.summary || "A connected sequence of reports, ordered by published time."}</p></div><button className="secondary-button" onClick={onBack}>Back to clusters</button></div><div className="timeline-state"><ClusterStateLabel state={cluster.curation_state} /><span>{cluster.item_count} reports / {cluster.source_count} sources</span></div>{cluster.items.length === 0 ? <EmptyClusters /> : <ol className="story-timeline">{cluster.items.map((item) => <li key={item.article_id}><div className="timeline-marker" /><div className="timeline-item"><div className="timeline-item-meta"><span>{formatStoryDate(item.published_at)}</span><span>{item.source_name}</span><span>{Math.round(item.similarity_score * 100)}% match</span></div><h3>{item.title}</h3><p>{item.summary || "Summary pending curation."}</p><a className="read-link" href={item.url} target="_blank" rel="noreferrer">Read report <span aria-hidden="true">↗</span></a></div></li>)}</ol>}</section>;
}

function ClusterStateLabel({ state }: { state: StoryCluster["curation_state"] }) {
  const labels: Record<StoryCluster["curation_state"], string> = { ready: "Ready", partial: "Partial", recalculating: "Recalculating", empty: "Empty" };
  return <span className={`cluster-state ${state}`}>{labels[state]}</span>;
}

function formatStoryDate(value: string | null): string {
  return value ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(new Date(value)) : "Date pending";
}

function SourcesView({ token }: { token: string }) {
  const [sources, setSources] = useState<SourceRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [tags, setTags] = useState("");
  const [pending, setPending] = useState(false);
  const [ingesting, setIngesting] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setError(null);
    void getSources(token).then((rows) => {
      if (active) setSources(rows);
    }).catch((requestError) => {
      if (active) setError(friendlyError(requestError));
    });
    return () => {
      active = false;
    };
  }, [token, attempt]);

  const add = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await createSource(token, {
        name: name.trim(),
        url: url.trim(),
        source_type: "rss",
        tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean),
        is_active: true,
      });
      setName("");
      setUrl("");
      setTags("");
      setNotice("Source added.");
      setAttempt((current) => current + 1);
    } catch (requestError) {
      setError(friendlyError(requestError));
    } finally {
      setPending(false);
    }
  };

  const toggleActive = async (source: SourceRecord) => {
    try {
      await updateSource(token, source.id, { is_active: !source.is_active });
      setAttempt((current) => current + 1);
    } catch (requestError) {
      setNotice(friendlyError(requestError));
    }
  };

  const remove = async (source: SourceRecord) => {
    try {
      await deleteSource(token, source.id);
      setNotice("Source removed.");
      setAttempt((current) => current + 1);
    } catch (requestError) {
      setNotice(friendlyError(requestError));
    }
  };

  const triggerIngestion = async (source: SourceRecord) => {
    setIngesting(source.id);
    setNotice(null);
    try {
      const result = await ingestSource(token, source.id);
      setNotice(`Fetch queued for ${source.name} (run ${result.run_id}).`);
    } catch (requestError) {
      setNotice(friendlyError(requestError));
    } finally {
      setIngesting(null);
    }
  };

  return (
    <section className="sources-view" aria-labelledby="sources-heading">
      <div className="sources-heading"><p className="eyebrow">FEED SOURCES / RSS</p><h1 id="sources-heading">Your sources.</h1><p className="lede">Add the feeds you want the desk to watch. Ingestion runs asynchronously and every source is validated before it is stored.</p></div>
      <form className="source-add-form" onSubmit={add}>
        <label className="briefing-field">Name<input value={name} onChange={(event) => setName(event.target.value)} required maxLength={200} placeholder="The Verge" /></label>
        <label className="briefing-field">RSS URL<input type="url" value={url} onChange={(event) => setUrl(event.target.value)} required maxLength={2048} placeholder="https://example.com/feed.xml" /></label>
        <label className="briefing-field">Tags (comma separated)<input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="tech, linux" /></label>
        <button className="primary-button" disabled={pending}>{pending ? "Adding…" : "Add source"}</button>
      </form>
      {error && <p className="form-error" role="alert">{error}</p>}
      {!sources && !error && <FeedLoading />}
      {sources && sources.length === 0 && <div className="empty-state"><span className="empty-index">NO SOURCES</span><h2>Nothing connected yet.</h2><p>Add your first RSS feed above.</p></div>}
      {sources && sources.length > 0 && (
        <ul className="source-list">
          {sources.map((source) => (
            <li key={source.id} className="source-row">
              <div className="source-info"><strong>{source.name}</strong><span>{source.url}</span><span className="source-meta">{source.origin} / {source.source_type} / {source.tags.join(", ") || "no tags"}{source.is_muted ? " / muted" : ""}</span></div>
              <div className="source-actions">
                <button className={source.is_active ? "feedback-button active" : "feedback-button"} onClick={() => void toggleActive(source)}>{source.is_active ? "Active" : "Paused"}</button>
                <button className="text-button" disabled={!source.is_active || ingesting === source.id} onClick={() => void triggerIngestion(source)}>{ingesting === source.id ? "Fetching…" : "Fetch now"}</button>
                <button className="text-button danger" onClick={() => void remove(source)}>Remove</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {notice && <div className="toast" role="status">{notice}</div>}
    </section>
  );
}

function AdminView({ token }: { token: string }) {
  const [section, setSection] = useState<"health" | "alerts" | "usage" | "users" | "audit" | "dev">("health");
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [alerts, setAlerts] = useState<AlertsReport | null>(null);
  const [usage, setUsage] = useState<UsageReport | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [audit, setAudit] = useState<Schemas["AuditEventExport"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"user" | "admin">("user");
  const [inviteToken, setInviteToken] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [devPending, setDevPending] = useState(false);
  const [devNotice, setDevNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setError(null);
    const load = async () => {
      try {
        if (section === "health") {
          const status = await getHealthStatus();
          if (active) setHealth(status);
        } else if (section === "alerts") {
          const report = await getAlerts(token);
          if (active) setAlerts(report);
        } else if (section === "usage") {
          const to = new Date();
          const from = new Date(to.getTime() - 7 * 86_400_000);
          const report = await getUsageReport(token, from.toISOString(), to.toISOString());
          if (active) setUsage(report);
        } else if (section === "users") {
          const rows = await getAdminUsers(token);
          if (active) setUsers(rows);
        } else {
          const rows = await getAuditEvents(token, 25);
          if (active) setAudit(rows);
        }
      } catch (requestError) {
        if (active) setError(friendlyError(requestError));
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [token, section, attempt]);

  const invite = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const created = await createInvitation(token, { email: inviteEmail, role: inviteRole, expires_in_hours: 72 });
      setInviteToken(created.token);
      setInviteEmail("");
    } catch (requestError) {
      setError(friendlyError(requestError));
    } finally {
      setPending(false);
    }
  };

  return (
    <section className="admin-view" aria-labelledby="admin-heading">
      <div className="admin-heading"><p className="eyebrow">OPERATIONS / ADMIN</p><h1 id="admin-heading">The control room.</h1><p className="lede">Health, alerts, usage and people. Every mutation is audited.</p></div>
      <div className="admin-tabs" role="tablist" aria-label="Operations sections">
        {(["health", "alerts", "usage", "users", "audit", "dev"] as const).map((name) => (
          <button key={name} className={section === name ? "view-tab active" : "view-tab"} onClick={() => setSection(name)} role="tab" aria-selected={section === name}>{name}</button>
        ))}
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {section === "health" && health && (
        <div className="admin-grid">
          <div className="profile-panel"><span className="panel-label">SYSTEM</span><p className="health-status-big">{health.status} / {health.readiness}</p><p className="muted">Checked {new Date(health.checked_at).toLocaleString()}</p></div>
          {Object.entries(health.checks).map(([name, check]) => (
            <div className="profile-panel" key={name}><span className="panel-label">{name.toUpperCase()}</span><p className={`health-status-big ${check.status}`}>{check.status}</p><p className="muted">{check.latency_ms} ms{check.detail ? ` / ${check.detail}` : ""}</p></div>
          ))}
        </div>
      )}
      {section === "alerts" && alerts && (
        <div className="admin-stack">
          <div className="profile-panel"><span className="panel-label">FIRING ({alerts.fired.length}) / SUPPRESSED {alerts.suppressed}</span>{alerts.fired.length === 0 ? <p className="muted">No alerts firing.</p> : alerts.fired.map((alert) => <AlertCard key={alert.rule} alert={alert} />)}</div>
          <div className="profile-panel"><span className="panel-label">RESOLVED ({alerts.resolved.length})</span>{alerts.resolved.map((alert) => <p className="muted" key={alert.rule}>{alert.rule}: {alert.message}</p>)}</div>
        </div>
      )}
      {section === "usage" && usage && (
        <div className="admin-stack">
          <div className="admin-grid">
            <div className="profile-panel"><span className="panel-label">EVENTS</span><p className="health-status-big">{usage.event_count}</p><p className="muted">{usage.fallback_event_count} fallbacks / {usage.estimated_event_count} estimated</p></div>
            <div className="profile-panel"><span className="panel-label">TOKENS</span><p className="health-status-big">{usage.input_tokens.toLocaleString()} / {usage.output_tokens.toLocaleString()}</p><p className="muted">input / output</p></div>
            <div className="profile-panel"><span className="panel-label">COST USD</span><p className="health-status-big">{usage.cost_usd.toFixed(4)}</p><p className="muted">7 day window</p></div>
            <div className="profile-panel"><span className="panel-label">LATENCY</span><p className="health-status-big">{usage.p95_latency_ms} ms p95</p><p className="muted">p99 {usage.p99_latency_ms} ms / avg {usage.average_latency_ms} ms</p></div>
          </div>
          {usage.breakdown.length > 0 && <div className="profile-panel"><span className="panel-label">BREAKDOWN</span>{usage.breakdown.map((row, index) => <p className="muted" key={index}>{row.provider} / {row.model} — {row.event_count} events, ${row.cost_usd.toFixed(4)}, p95 {row.p95_latency_ms} ms</p>)}</div>}
        </div>
      )}
      {section === "users" && users && (
        <div className="admin-stack">
          <form className="invite-form" onSubmit={invite}>
            <label className="briefing-field">Email<input type="email" value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} required placeholder="new@member.com" /></label>
            <label className="briefing-field">Role<select value={inviteRole} onChange={(event) => setInviteRole(event.target.value as "user" | "admin")}><option value="user">user</option><option value="admin">admin</option></select></label>
            <button className="primary-button" disabled={pending}>{pending ? "Inviting…" : "Invite"}</button>
          </form>
          {inviteToken && <div className="invite-token"><span className="panel-label">INVITATION TOKEN (single use)</span><code>{inviteToken}</code><p className="muted">Share this link-style token with the invitee; it expires in 72 hours.</p></div>}
          <div className="profile-panel"><span className="panel-label">USERS</span>{users.map((user) => <p className="muted" key={user.id}>{user.email} — {user.role}{user.is_active ? "" : " / suspended"}</p>)}</div>
        </div>
      )}
      {section === "audit" && audit && (
        <div className="admin-stack">
          <div className="profile-panel"><span className="panel-label">RECENT AUDIT</span>{audit.items.length === 0 ? <p className="muted">No audit events.</p> : audit.items.map((event) => <p className="muted" key={event.id}>{String(event.created_at).slice(0, 19)} — {event.action} / {event.outcome}{event.actor_id ? ` / ${event.actor_id}` : ""}</p>)}</div>
        </div>
      )}
      {section === "dev" && (
        <div className="admin-stack">
          <div className="profile-panel">
            <span className="panel-label">LOCAL DEVELOPMENT</span>
            <h2>Move extracted stories into the feed.</h2>
            <p className="muted">Use this only in a local deployment. Production curation should advance articles through the normal pipeline.</p>
            <button className="primary-button" disabled={devPending} onClick={() => {
              setDevPending(true);
              setDevNotice(null);
              setError(null);
              void devPublishExtracted(token).then((result) => {
                setDevNotice(`${result.published} extracted ${result.published === 1 ? "article" : "articles"} published.`);
              }).catch((requestError) => {
                setDevNotice(friendlyError(requestError));
              }).finally(() => setDevPending(false));
            }}>{devPending ? "Publishing…" : "Publish extracted articles"}</button>
            {devNotice && <p className="muted" role="status">{devNotice}</p>}
          </div>
        </div>
      )}
      {!error && ((section === "health" && !health) || (section === "alerts" && !alerts) || (section === "usage" && !usage) || (section === "users" && !users) || (section === "audit" && !audit)) && <FeedLoading />}
    </section>
  );
}

function AlertCard({ alert }: { alert: AlertsReport["fired"][number] }) {
  return <div className="alert-card"><strong>{alert.rule}</strong><span className={`cluster-state ${alert.severity === "critical" ? "recalculating" : "partial"}`}>{alert.severity}</span><p>{alert.message}</p></div>;
}

function BriefingsView({
  briefings,
  schedule,
  loading,
  error,
  onRetry,
  onScheduleUpdate,
  onGenerate,
  onRegenerate,
}: {
  briefings: Briefing[];
  schedule: BriefingSchedule | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onScheduleUpdate: (payload: Omit<BriefingSchedule, "id">) => Promise<void>;
  onGenerate: () => Promise<void>;
  onRegenerate: (briefingId: number) => Promise<void>;
}) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [localTime, setLocalTime] = useState("08:00");
  const [timezone, setTimezone] = useState("UTC");
  const [isActive, setIsActive] = useState(true);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setSelectedId((current) => current !== null && briefings.some((briefing) => briefing.id === current) ? current : briefings[0]?.id ?? null);
  }, [briefings]);
  useEffect(() => {
    if (!schedule) return;
    setLocalTime(schedule.local_time);
    setTimezone(schedule.timezone);
    setIsActive(schedule.is_active);
  }, [schedule]);
  const selected = briefings.find((briefing) => briefing.id === selectedId) ?? null;
  const saveSchedule = () => {
    setSaving(true);
    void onScheduleUpdate({ local_time: localTime, timezone, is_active: isActive }).finally(() => setSaving(false));
  };
  return <section className="briefings-view" aria-labelledby="briefings-heading"><div className="briefings-heading"><div><p className="eyebrow">DAILY EDITIONS / SOURCE-AWARE</p><h1 id="briefings-heading">The morning brief.</h1><p className="lede">A durable edition with provenance, a local clock, and a safe fallback when the model is unavailable.</p></div><button className="primary-button" onClick={() => void onGenerate()} disabled={!schedule || loading}>{loading ? "Loading…" : "Generate today"}</button></div>{error && <ErrorState message={error} onRetry={onRetry} />}{!error && loading && <FeedLoading />}{!error && !loading && <div className="briefings-layout"><aside className="briefing-sidebar"><div className="briefing-panel"><span className="panel-label">SCHEDULE</span><label className="briefing-field">Local time<input type="time" value={localTime} onChange={(event) => setLocalTime(event.target.value)} /></label><label className="briefing-field">Timezone<input value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="Europe/Madrid" /></label><label className="toggle-field"><input type="checkbox" checked={isActive} onChange={(event) => setIsActive(event.target.checked)} /> Active schedule</label><button className="text-button" disabled={saving || !schedule}>{saving ? "Saving…" : "Save schedule"}</button></div><div className="briefing-panel"><span className="panel-label">ARCHIVE</span>{briefings.length === 0 ? <p className="muted">No editions yet.</p> : <div className="briefing-history">{briefings.map((briefing) => <button key={briefing.id} className={briefing.id === selectedId ? "briefing-history-item active" : "briefing-history-item"} onClick={() => setSelectedId(briefing.id)}><span>{briefing.briefing_date}</span><strong>{briefing.title}</strong><ClusterStateLabel state={briefing.status === "ready" ? "ready" : briefing.status === "failed" ? "recalculating" : "partial"} /></button>)}</div>}</div></aside><div className="briefing-reader">{selected ? <BriefingReader briefing={selected} onRegenerate={onRegenerate} /> : <EmptyBriefings />}</div></div>}</section>;
}

function BriefingReader({ briefing, onRegenerate }: { briefing: Briefing; onRegenerate: (briefingId: number) => Promise<void> }) {
  return <article className="briefing-reader-card"><div className="briefing-reader-meta"><span>{briefing.briefing_date} / {briefing.local_time}</span><span>{briefing.timezone}</span><span>REVISION {briefing.revision}</span><span className={`briefing-status ${briefing.status}`}>{briefing.status}</span></div><div className="briefing-reader-heading"><h2>{briefing.title}</h2><button className="secondary-button" onClick={() => void onRegenerate(briefing.id)}>Regenerate</button></div><pre className="briefing-content">{briefing.content_markdown}</pre>{briefing.error_message && <p className="briefing-warning" role="status">Fallback used: {briefing.error_message}</p>}<div className="briefing-provenance"><span className="panel-label">PROVENANCE / {briefing.items.length} REPORTS</span>{briefing.items.map((item) => { const title = typeof item.provenance_json.title === "string" ? item.provenance_json.title : `Article ${item.article_id}`; const source = typeof item.provenance_json.source_name === "string" ? item.provenance_json.source_name : "Unknown source"; return <div className="provenance-row" key={item.article_id}><span>{String(item.position).padStart(2, "0")}</span><strong>{title}</strong><em>{source} / display {Math.round(item.display_rank * 100)}</em></div>; })}</div></article>;
}

function EmptyBriefings() {
  return <div className="empty-state" role="status"><span className="empty-index">NO EDITION</span><h2>Your first briefing is waiting.</h2><p>Set the local schedule and generate today’s edition from published stories.</p></div>;
}

function ProfileView({
  profile,
  error,
  settings,
  settingsError,
  onRankingUpdate,
  onSettingsUpdate,
  onTopicUpdate,
  onTopicReset,
  onSourceMute,
  onSourceReset,
}: {
  profile: PreferenceProfile | null;
  error: string | null;
  settings: UserSettings | null;
  settingsError: string | null;
  onRankingUpdate: (serendipity: number) => Promise<void>;
  onSettingsUpdate: (payload: UserSettingsUpdate) => Promise<void>;
  onTopicUpdate: (topicName: string, weightScore: number) => Promise<void>;
  onTopicReset: (topicName: string) => Promise<void>;
  onSourceMute: (sourceId: number, isMuted: boolean) => Promise<void>;
  onSourceReset: (sourceId: number) => Promise<void>;
}) {
  return (
    <section className="profile-view" aria-labelledby="profile-heading">
      <div className="profile-heading"><p className="eyebrow">LEARNED PROFILE / TRANSPARENT BY DESIGN</p><h1 id="profile-heading">Your signal.</h1><p className="lede">A quiet snapshot of what the desk has learned. Fine controls arrive with your first feedback.</p></div>
      {error && <p className="form-error" role="alert">{error}</p>}
       {!error && !profile && <FeedLoading />}
       {profile && <><MindmapPanel topics={profile.topics} sources={profile.sources} onTopicUpdate={onTopicUpdate} onTopicReset={onTopicReset} /><div className="profile-grid"><div className="profile-panel"><span className="panel-label">TOPICS</span>{profile.topics.length === 0 ? <p className="muted">No topic preferences yet.</p> : profile.topics.map((topic) => <TopicControl key={topic.id} topic={topic} onUpdate={onTopicUpdate} onReset={onTopicReset} />)}</div><div className="profile-panel"><span className="panel-label">SOURCES</span>{profile.sources.length === 0 ? <p className="muted">No sources connected yet.</p> : profile.sources.map((source) => <SourceControl key={source.id} source={source} onMute={onSourceMute} onReset={onSourceReset} />)}</div><RankingControl ranking={profile.ranking} onUpdate={onRankingUpdate} />{settings && <SettingsControl settings={settings} onUpdate={onSettingsUpdate} />}{settingsError && <p className="form-error" role="alert">{settingsError}</p>}<div className="profile-panel profile-version"><span className="panel-label">PROFILE VERSION</span><strong>{profile.profile_version}</strong><p className="muted">Changes are versioned and auditable.</p></div></div></>}
    </section>
  );
}

function MindmapPanel({
  topics,
  sources,
  onTopicUpdate,
  onTopicReset,
}: {
  topics: TopicPreference[];
  sources: SourcePreference[];
  onTopicUpdate: (topicName: string, weightScore: number) => Promise<void>;
  onTopicReset: (topicName: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState<TopicPreference | null>(null);
  const [draft, setDraft] = useState(0);
  const openEditor = (topic: TopicPreference) => {
    setEditing(topic);
    setDraft(topic.weight_score);
  };
  const nodes = topics.slice(0, 14);
  const center = 170;
  const ring = 128;
  const positions = nodes.map((topic, index) => {
    const angle = (index / Math.max(nodes.length, 1)) * 2 * Math.PI - Math.PI / 2;
    const magnitude = Math.max(-1, Math.min(1, topic.weight_score));
    const size = 40 + Math.abs(magnitude) * 66;
    return {
      topic,
      x: center + ring * Math.cos(angle),
      y: center + ring * Math.sin(angle),
      size,
      negative: magnitude < 0,
    };
  });
  const visibleSources = sources.filter((source) => !source.is_muted);
  const mutedSources = sources.filter((source) => source.is_muted);
  return (
    <section className="mindmap-panel" aria-labelledby="mindmap-heading">
      <div className="mindmap-heading"><span className="panel-label">MINDMAP / LEARNED TOPICS</span><h2 id="mindmap-heading">What the desk hears.</h2>{editing && <div className="mindmap-edit"><strong>{editing.topic_name}</strong><label>Weight<input type="range" min={-1} max={1} step={0.05} value={draft} onChange={(event) => setDraft(Number(event.target.value))} /></label><span className="muted">{draft.toFixed(2)}</span><button className="text-button" onClick={() => void onTopicUpdate(editing.topic_name, draft).then(() => setEditing(null))}>Apply weight</button><button className="text-button danger" onClick={() => void onTopicReset(editing.topic_name).then(() => setEditing(null))}>Forget topic</button></div>}</div>
      {nodes.length === 0
        ? <p className="muted">Your signal map is empty. Rate stories to grow it.</p>
        : <svg className="mindmap-svg" viewBox="0 0 340 340" role="img" aria-label="Topic mindmap">
            {positions.map(({ topic, x, y, size, negative }) => (
              <g key={topic.id}>
                <line x1={center} y1={center} x2={x} y2={y} className={negative ? "mindmap-edge negative" : "mindmap-edge"} strokeWidth={0.5 + Math.abs(Math.max(-1, Math.min(1, topic.weight_score))) * 2.2} />
                <g className="mindmap-node" transform={`translate(${x} ${y})`} onClick={() => openEditor(topic)} role="button" tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") openEditor(topic); }}>
                  <circle r={size / 2} className={negative ? "negative" : ""} />
                  <text textAnchor="middle" dominantBaseline="middle">{topic.topic_name.length > 16 ? `${topic.topic_name.slice(0, 15)}…` : topic.topic_name}</text>
                </g>
              </g>
            ))}
            <g className="mindmap-center">
              <circle r={34} />
              <text textAnchor="middle" dominantBaseline="middle">SIGNAL</text>
            </g>
          </svg>}
      <div className="mindmap-source-rail" aria-label="Source reputation">
        <span className="panel-label">SOURCE REPUTATION</span>
        {visibleSources.slice(0, 10).map((source) => (
          <div className="mindmap-source" key={source.id}><span>{source.name}</span><span className="source-track"><span style={{ width: `${Math.round(source.reputation_score * 100)}%` }} /></span></div>
        ))}
        {mutedSources.slice(0, 10).map((source) => (
          <div className="mindmap-source muted" key={source.id}><span>{source.name}</span><span className="source-track"><span style={{ width: "0%" }} /></span></div>
        ))}
        {sources.length === 0 && <p className="muted">No sources yet.</p>}
      </div>
    </section>
  );
}

function SettingsControl({ settings, onUpdate }: { settings: UserSettings; onUpdate: (payload: UserSettingsUpdate) => Promise<void> }) {
  const [profile, setProfile] = useState<UserSettings["llm_profile"]>(settings.llm_profile);
  const [queries, setQueries] = useState(settings.discovery_max_queries);
  const [candidates, setCandidates] = useState(settings.discovery_max_candidates);
  const [briefingItems, setBriefingItems] = useState(settings.briefing_max_items);
  const [semantic, setSemantic] = useState(settings.score_weights.semantic);
  const [reputation, setReputation] = useState(settings.score_weights.reputation);
  const [feedback, setFeedback] = useState(settings.score_weights.feedback);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    setProfile(settings.llm_profile);
    setQueries(settings.discovery_max_queries);
    setCandidates(settings.discovery_max_candidates);
    setBriefingItems(settings.briefing_max_items);
    setSemantic(settings.score_weights.semantic);
    setReputation(settings.score_weights.reputation);
    setFeedback(settings.score_weights.feedback);
  }, [settings]);

  const save = () => {
    setPending(true);
    void onUpdate({
      version: settings.version,
      llm_profile: profile,
      discovery_max_queries: queries,
      discovery_max_candidates: candidates,
      briefing_max_items: briefingItems,
      score_words_per_minute: settings.score_words_per_minute,
      score_weights: { semantic, reputation, feedback },
    }).finally(() => setPending(false));
  };

  return <div className="profile-panel settings-panel"><div className="settings-heading"><div><span className="panel-label">PERSONAL SETTINGS</span><p className="muted">Version {settings.version}. Defaults stay private to your account.</p></div><span className="settings-badge">SAFE CAPS</span></div><label className="briefing-field">Briefing model<select value={profile} onChange={(event) => setProfile(event.target.value as UserSettings["llm_profile"])}><option value="eco">Eco / local</option><option value="hybrid">Hybrid</option><option value="cloud">Cloud</option></select></label><div className="settings-grid"><label className="briefing-field">Discovery queries<input type="number" min="1" value={queries} onChange={(event) => setQueries(Number(event.target.value))} /></label><label className="briefing-field">Candidate cap<input type="number" min="1" value={candidates} onChange={(event) => setCandidates(Number(event.target.value))} /></label><label className="briefing-field">Briefing items<input type="number" min="1" value={briefingItems} onChange={(event) => setBriefingItems(Number(event.target.value))} /></label></div><div className="settings-grid"><label className="briefing-field">Semantic weight<input type="number" min="0" max="1" step="0.05" value={semantic} onChange={(event) => setSemantic(Number(event.target.value))} /></label><label className="briefing-field">Reputation weight<input type="number" min="0" max="1" step="0.05" value={reputation} onChange={(event) => setReputation(Number(event.target.value))} /></label><label className="briefing-field">Feedback weight<input type="number" min="0" max="1" step="0.05" value={feedback} onChange={(event) => setFeedback(Number(event.target.value))} /></label></div><button className="text-button" disabled={pending} onClick={() => void save()}>{pending ? "Saving…" : "Save personal settings"}</button></div>;
}

function RankingControl({ ranking, onUpdate }: { ranking: PreferenceProfile["ranking"]; onUpdate: (serendipity: number) => Promise<void> }) {
  const [value, setValue] = useState(ranking.serendipity);
  const [pending, setPending] = useState(false);
  useEffect(() => setValue(ranking.serendipity), [ranking.serendipity]);
  return <div className="profile-panel ranking-panel"><span className="panel-label">DISPLAY MIX</span><p className="muted ranking-copy">Choose how much the desk makes room for unfamiliar sources and topics.</p><div className="preference-control"><div className="preference-row"><span>Serendipity</span><strong>{Math.round(value * 100)}%</strong></div><label className="range-label"><span className="sr-only">Serendipity mix</span><input type="range" min="0" max="1" step="0.05" value={value} onChange={(event) => setValue(Number(event.target.value))} /></label><div className="range-scale"><span>RELEVANCE</span><span>EXPLORE</span></div><div className="control-actions"><button className="text-button" disabled={pending || value === ranking.serendipity} onClick={() => { setPending(true); void onUpdate(value).finally(() => setPending(false)); }}>{pending ? "Saving…" : "Save mix"}</button></div></div></div>;
}

function TopicControl({ topic, onUpdate, onReset }: { topic: PreferenceProfile["topics"][number]; onUpdate: (topicName: string, weightScore: number) => Promise<void>; onReset: (topicName: string) => Promise<void> }) {
  const [value, setValue] = useState(topic.weight_score);
  const [pending, setPending] = useState(false);
  return <div className="preference-control"><div className="preference-row"><span>{topic.topic_name}</span><strong>{value > 0 ? "+" : ""}{value.toFixed(2)}</strong></div><label className="range-label"><span className="sr-only">Weight for {topic.topic_name}</span><input type="range" min="-1" max="1" step="0.05" value={value} onChange={(event) => setValue(Number(event.target.value))} /></label><div className="control-actions"><button className="text-button" disabled={pending || value === topic.weight_score} onClick={() => { setPending(true); void onUpdate(topic.topic_name, value).finally(() => setPending(false)); }}>{pending ? "Saving…" : "Save"}</button><button className="text-button danger" disabled={pending} onClick={() => { if (window.confirm(`Reset ${topic.topic_name} preference? Feedback history stays archived.`)) { setPending(true); void onReset(topic.topic_name).finally(() => setPending(false)); } }}>Reset</button></div></div>;
}

function SourceControl({ source, onMute, onReset }: { source: PreferenceProfile["sources"][number]; onMute: (sourceId: number, isMuted: boolean) => Promise<void>; onReset: (sourceId: number) => Promise<void> }) {
  const [pending, setPending] = useState(false);
  return <div className="preference-control"><div className="preference-row"><span>{source.name}</span><strong>{source.is_muted ? "Muted" : `${Math.round(source.reputation_score * 100)} signal`}</strong></div><div className="control-actions"><button className="text-button" disabled={pending} onClick={() => { setPending(true); void onMute(source.id, !source.is_muted).finally(() => setPending(false)); }}>{pending ? "Saving…" : source.is_muted ? "Restore" : "Mute source"}</button><button className="text-button danger" disabled={pending} onClick={() => { if (window.confirm(`Reset ${source.name} preference? Feedback history stays archived.`)) { setPending(true); void onReset(source.id).finally(() => setPending(false)); } }}>Reset</button></div></div>;
}

function FeedLoading() {
  return <div className="loading-grid" aria-label="Loading stories" aria-busy="true">{[1, 2, 3].map((item) => <div className="skeleton-card" key={item}><span /><span /><span /></div>)}</div>;
}

function EmptyFeed({ hasFilter }: { hasFilter: boolean }) {
  return <div className="empty-state" role="status"><span className="empty-index">{hasFilter ? "NO MATCH" : "NEXT"}</span><h2>{hasFilter ? "Nothing in that lane yet." : "Your first stories are waiting."}</h2><p>{hasFilter ? "Try another tag or clear the filter to widen the desk." : "Add an RSS source through the API and your private reading desk will start taking shape."}</p></div>;
}

function EmptyClusters() {
  return <div className="empty-state cluster-empty" role="status"><span className="empty-index">NO STORIES</span><h2>No connected stories yet.</h2><p>Switch to the flat desk to read individual reports while the story graph catches up.</p></div>;
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div className="error-state" role="alert"><span className="empty-index">SIGNAL LOST</span><h2>The desk could not load.</h2><p>{message}</p><button className="secondary-button" onClick={onRetry}>Try again</button></div>;
}
