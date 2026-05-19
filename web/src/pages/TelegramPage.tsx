import { useEffect, useLayoutEffect, useState, useCallback } from "react";
import {
  MessageCircle,
  RefreshCw,
  Wifi,
  WifiOff,
  AlertTriangle,
  Bot,
  Users,
  Settings,
  KeyRound,
  Radio,
  Globe,
  Hash,
  Clock,
  Send,
} from "lucide-react";
import { api } from "@/lib/api";
import type { PlatformStatus, SessionInfo } from "@/lib/api";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { PluginSlot } from "@/plugins";

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function classifyPlatformState(
  state: string,
): "success" | "warning" | "destructive" | "outline" {
  switch (state) {
    case "connected":
      return "success";
    case "disconnected":
      return "warning";
    case "fatal":
      return "destructive";
    default:
      return "outline";
  }
}

function StateIcon({ state }: { state: string }) {
  if (state === "connected")
    return <Wifi className="h-4 w-4 text-success" />;
  if (state === "fatal")
    return <AlertTriangle className="h-4 w-4 text-destructive" />;
  return <WifiOff className="h-4 w-4 text-warning" />;
}

/** Mask a bot token for display: show first 8 chars only. */
function maskToken(token: string): string {
  if (token.length <= 8) return "••••••••";
  return token.slice(0, 8) + "••••••••";
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export default function TelegramPage() {
  const { t } = useI18n();
  const { setAfterTitle, setEnd } = usePageHeader();

  // Status data
  const [status, setStatus] = useState<{
    platform: PlatformStatus | null;
    gatewayRunning: boolean;
  } | null>(null);

  // Config data
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);

  // Env data
  const [env, setEnv] = useState<Record<string, unknown> | null>(null);

  // Telegram sessions
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [totalSessions, setTotalSessions] = useState(0);

  // Loading / error
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /* ---- fetch all data ---- */
  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [statusRes, configRes, envRes, sessionsRes] = await Promise.all([
        api.getStatus(),
        api.getConfig().catch(() => null),
        api.getEnvVars().catch(() => null),
        api.getSessions(100, 0),
      ]);

      const tgPlatform: PlatformStatus | null =
        statusRes.gateway_platforms?.telegram ?? null;

      setStatus({
        platform: tgPlatform,
        gatewayRunning: statusRes.gateway_running,
      });
      setConfig(configRes);
      setEnv(envRes);

      // Filter sessions to telegram-originated ones
      const tgSessions = (sessionsRes.sessions ?? []).filter(
        (s: SessionInfo) => s.source === "telegram",
      );
      setSessions(tgSessions);
      setTotalSessions(sessionsRes.total ?? 0);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useLayoutEffect(() => {
    setAfterTitle?.(
      <Badge tone="outline" className="text-xs">
        {t.telegram.title}
      </Badge>,
    );
    setEnd?.(
      <Button
        ghost
        size="icon"
        onClick={fetchAll}
        aria-label={t.common.refresh}
      >
        <RefreshCw className="h-4 w-4" />
      </Button>,
    );
  }, [setAfterTitle, setEnd, fetchAll, t]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  /* ---- derive telegram config ---- */
  const tgConfig =
    config && typeof config === "object" && "platforms" in config
      ? ((config.platforms as Record<string, unknown>)?.telegram as
          | Record<string, unknown>
          | undefined) ?? null
      : null;

  const allowedUserIds: number[] =
    (tgConfig?.allowed_user_ids as number[]) ??
    (tgConfig?.allowed_user_ids as unknown[])?.map(Number) ??
    [];

  const groupChats: Record<string, unknown>[] =
    (tgConfig?.group_chats as Record<string, unknown>[]) ?? [];

  const botTokenEnv = env
    ? ((env["TELEGRAM_BOT_TOKEN"] ?? env["TG_BOT_TOKEN"]) as
        | { is_set: boolean; redacted_value: string | null }
        | undefined)
    : null;

  /* ---- render ---- */
  if (loading && !status) {
    return (
      <div className="flex items-center justify-center py-16">
        <Spinner />
      </div>
    );
  }

  if (error && !status) {
    return (
      <div className="flex flex-col items-center gap-4 py-16">
        <AlertTriangle className="h-8 w-8 text-destructive" />
        <p className="text-sm text-destructive">{error}</p>
        <Button onClick={fetchAll}>{t.common.retry}</Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 p-4 sm:p-6">
      {/* ---- Status Card ---- */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Radio className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">
              {t.telegram.connectionStatus}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {status?.platform ? (
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <StateIcon state={status.platform.state} />
                <div>
                  <p className="text-sm font-medium">
                    {status.platform.state === "connected"
                      ? t.status.connected
                      : status.platform.state === "fatal"
                        ? t.status.error
                        : t.status.disconnected}
                  </p>
                  {status.platform.error_message && (
                    <p className="text-xs text-destructive">
                      {status.platform.error_message}
                    </p>
                  )}
                  {status.platform.updated_at && (
                    <p className="text-xs text-muted-foreground">
                      {t.status.lastUpdate}:{" "}
                      {new Date(
                        status.platform.updated_at,
                      ).toLocaleString()}
                    </p>
                  )}
                </div>
              </div>
              <Badge tone={classifyPlatformState(status.platform.state)}>
                {status.platform.state}
              </Badge>
            </div>
          ) : (
            <div className="flex items-center gap-3">
              <WifiOff className="h-4 w-4 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">
                {status?.gatewayRunning
                  ? t.telegram.notConfigured
                  : t.status.notRunning}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* ---- Bot Config Card ---- */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Bot className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">
              {t.telegram.botConfig}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          {/* Bot Token */}
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              <KeyRound className="h-3 w-3" />
              {t.telegram.botToken}
            </span>
            <span className="text-sm font-mono">
              {botTokenEnv?.is_set
                ? maskToken(
                    botTokenEnv.redacted_value ?? "••••••••••••••••",
                  )
                : "—"}
            </span>
            {botTokenEnv && (
              <Badge
                tone={botTokenEnv.is_set ? "success" : "destructive"}
                className="w-fit mt-1"
              >
                {botTokenEnv.is_set
                  ? t.common.configured
                  : t.common.notConfigured}
              </Badge>
            )}
          </div>

          {/* Allowed Users */}
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              <Users className="h-3 w-3" />
              {t.telegram.allowedUsers}
            </span>
            <span className="text-sm">
              {allowedUserIds.length > 0
                ? allowedUserIds.join(", ")
                : t.common.none}
            </span>
            {allowedUserIds.length > 0 && (
              <Badge tone="outline" className="w-fit mt-1">
                {allowedUserIds.length}{" "}
                {allowedUserIds.length === 1 ? "user" : "users"}
              </Badge>
            )}
          </div>

          {/* Group Chats */}
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              <Hash className="h-3 w-3" />
              {t.telegram.groupChats}
            </span>
            <span className="text-sm">
              {groupChats.length > 0
                ? groupChats
                    .map((g) => String(g.title ?? g.id ?? g.chat_id ?? ""))
                    .join(", ")
                : t.common.none}
            </span>
            {groupChats.length > 0 && (
              <Badge tone="outline" className="w-fit mt-1">
                {groupChats.length}{" "}
                {groupChats.length === 1 ? "group" : "groups"}
              </Badge>
            )}
          </div>

          {/* Gateway Status */}
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              <Radio className="h-3 w-3" />
              {t.telegram.gateway}
            </span>
            <span className="text-sm">
              {status?.gatewayRunning
                ? t.status.running
                : t.status.notRunning}
            </span>
            <Badge
              tone={status?.gatewayRunning ? "success" : "warning"}
              className="w-fit mt-1"
            >
              {status?.gatewayRunning ? t.status.running : t.status.stopped}
            </Badge>
          </div>
        </CardContent>
      </Card>

      {/* ---- Recent Telegram Sessions Card ---- */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <MessageCircle className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">
              {t.telegram.recentSessions}
            </CardTitle>
            <Badge tone="outline" className="ml-auto text-xs">
              {sessions.length}/{totalSessions}
            </Badge>
          </div>
        </CardHeader>
        <CardContent>
          {sessions.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t.telegram.noSessions}
            </p>
          ) : (
            <div className="grid gap-2">
              {sessions.slice(0, 10).map((s) => (
                <div
                  key={s.id}
                  className="flex items-center justify-between border border-border p-2 rounded"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <MessageCircle className="h-3.5 w-3.5 shrink-0 text-primary/60" />
                    <div className="min-w-0">
                      <p className="text-sm truncate">
                        {s.title || t.common.untitled}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {s.message_count} {t.common.msgs} ·{" "}
                        {s.tool_call_count} {t.common.tools}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <Badge
                      tone={s.is_active ? "success" : "outline"}
                      className="text-[10px]"
                    >
                      {s.is_active
                        ? t.common.active
                        : t.common.inactive}
                    </Badge>
                    {s.started_at && (
                      <span className="text-[10px] text-muted-foreground whitespace-nowrap">
                        {new Date(s.started_at * 1000).toLocaleDateString()}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ---- How to get started ---- */}
      {!status?.platform && !loading && (
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Globe className="h-5 w-5 text-muted-foreground" />
              <CardTitle className="text-base">
                {t.telegram.gettingStarted}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            <ol className="list-decimal list-inside text-sm text-muted-foreground space-y-2">
              <li>
                {t.telegram.gsCreateBot}&nbsp;
                <code className="text-xs bg-muted px-1 py-0.5 rounded">
                  @BotFather
                </code>
              </li>
              <li>{t.telegram.gsSetToken}</li>
              <li>{t.telegram.gsSetUsers}</li>
              <li>
                {t.telegram.gsRestart}
              </li>
            </ol>
          </CardContent>
        </Card>
      )}

      <PluginSlot name="telegram-bottom" />
    </div>
  );
}
