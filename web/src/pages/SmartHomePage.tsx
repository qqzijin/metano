import { useState } from "react";
import { Home, Lightbulb, Power, PowerOff, AlertTriangle, Settings2 } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { RoleGuard } from "@/components/auth/RoleGuard";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchAPI } from "@/api/client";
import { toast } from "sonner";

interface HAType {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
}

interface HomeStatus {
  configured: boolean;
  entities: HAType[];
  error?: string;
  message?: string;
}

interface HAConfig {
  url: string;
  token_set: boolean;
}

export default function SmartHomePage() {
  const [filter, setFilter] = useState("");
  const queryClient = useQueryClient();

  const { data, isLoading, isError } = useQuery<HomeStatus>({
    queryKey: ["home", "status"],
    queryFn: () => fetchAPI("/home/status"),
    refetchInterval: 15000,
  });

  const configured = data?.configured !== false;

  const configQuery = useQuery<HAConfig>({
    queryKey: ["home", "config"],
    queryFn: () => fetchAPI("/home/config"),
    enabled: !configured,
  });

  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const syncFromServer = () => {
    const serverUrl = configQuery.data?.url;
    if (serverUrl) {
      setUrl((prev) => prev || serverUrl);
    }
  };
  const saveConfig = useMutation({
    mutationFn: (values: { url: string; token: string }) =>
      fetchAPI("/home/config", {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: () => {
      toast.success("配置已保存，正在连接 Home Assistant…");
      queryClient.invalidateQueries({ queryKey: ["home", "status"] });
      queryClient.invalidateQueries({ queryKey: ["home", "config"] });
    },
    onError: () => toast.error("保存失败，请重试"),
  });

  const entities = data?.entities ?? [];
  const domains = [...new Set(entities.map((e) => e.entity_id.split(".")[0]))];
  const filtered = filter
    ? entities.filter((e) => e.entity_id.startsWith(filter + "."))
    : entities;

  const handleControl = async (entityId: string, service: string) => {
    try {
      // M-01: the backend returns HTTP 200 + {error} for a failed HA call —
      // treat that as a failure instead of toasting "成功".
      const res = await fetchAPI<{ entity_id: string; action: string; result?: unknown; error?: string }>("/home/control", {
        method: "POST",
        body: JSON.stringify({ entity_id: entityId, service }),
      });
      if (res?.error) {
        toast.error(`${entityId}: ${res.error}`);
        return;
      }
      toast.success(`${entityId} → ${service}`);
      // M-05: reflect the new state immediately instead of waiting for the 15s
      // status poll (and optimistically flip the toggle button).
      queryClient.invalidateQueries({ queryKey: ["home", "status"] });
      queryClient.setQueryData<HomeStatus>(["home", "status"], (old) => {
        if (!old) return old;
        const target = service === "turn_on" ? "on" : service === "turn_off" ? "off" : "on";
        return {
          ...old,
          entities: (old.entities ?? []).map((e) =>
            e.entity_id === entityId ? { ...e, state: target } : e
          ),
        };
      });
    } catch {
      toast.error("控制失败");
    }
  };

  const handleSaveConfig = () => {
    if (!url.trim() && !token.trim()) {
      toast.error("请填写 HA_URL 或 HA_TOKEN 至少一项");
      return;
    }
    saveConfig.mutate({ url: url.trim(), token: token.trim() });
  };

  const headerDesc = !configured
    ? "未配置 Home Assistant"
    : entities.length === 0
      ? data?.error
        ? "连接 Home Assistant 失败"
        : "已配置，暂无设备"
      : `${entities.length} 个设备`;

  return (
    <RoleGuard role="admin">
      <PageHeader title="智能家居" description={headerDesc} />

      {!configured ? (
        <div className="mx-auto w-full max-w-md">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base flex items-center gap-2">
                <Settings2 className="size-4.5 text-primary" /> 未配置 Home Assistant
              </CardTitle>
              <CardDescription>需要 HA_URL 与 HA_TOKEN 才能连接智能设备</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="text-sm text-muted-foreground space-y-1">
                <p>配置方式（任选其一）：</p>
                <ol className="list-decimal list-inside space-y-0.5">
                  <li>在下方表单填写并保存（写入 gateway_config.yaml）</li>
                  <li>编辑 gateway_config.yaml 的 <code className="text-foreground break-all">home_assistant</code> 段</li>
                  <li>设置环境变量 HA_URL / HA_TOKEN 后重启服务</li>
                </ol>
              </div>

              <div className="space-y-2">
                <div className="min-w-0">
                  <label className="text-xs font-medium text-muted-foreground">HA_URL</label>
                  <Input
                    className="mt-1"
                    placeholder="http://homeassistant.local:8123"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    onBlur={syncFromServer}
                  />
                </div>
                <div className="min-w-0">
                  <label className="text-xs font-medium text-muted-foreground">
                    HA_TOKEN {configQuery.data?.token_set ? "（已设置，留空保持不变）" : ""}
                  </label>
                  <Input
                    className="mt-1"
                    type="password"
                    placeholder={configQuery.data?.token_set ? "••••••••（已设置）" : "长期访问令牌"}
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                  />
                </div>
                <Button
                  className="w-full"
                  onClick={handleSaveConfig}
                  disabled={saveConfig.isPending}
                >
                  {saveConfig.isPending ? "保存中…" : "保存配置"}
                </Button>
              </div>

              {data?.message ? (
                <p className="text-xs text-muted-foreground">{data.message}</p>
              ) : null}
            </CardContent>
          </Card>
        </div>
      ) : isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-20 rounded-lg" />)}
        </div>
      ) : entities.length === 0 ? (
        data?.error ? (
          <EmptyState
            title="无法连接 Home Assistant"
            description={`${data.error}。请检查 gateway_config.yaml 中的 HA_URL / HA_TOKEN 是否正确，且 Home Assistant 已启动。`}
            icon={<AlertTriangle className="size-10" />}
          />
        ) : (
          <EmptyState
            title="暂无设备"
            description="Home Assistant 已连接，但没有任何设备。请检查 HA 中的实体。"
            icon={<Home className="size-10" />}
          />
        )
      ) : (
        <>
          <div className="flex gap-2 mb-4 flex-wrap">
            <Button size="sm" variant={filter === "" ? "default" : "outline"} onClick={() => setFilter("")}>全部</Button>
            {domains.map((d) => (
              <Button key={d} size="sm" variant={filter === d ? "default" : "outline"} onClick={() => setFilter(d)}>
                {d}
              </Button>
            ))}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {filtered.map((e) => {
              const domain = e.entity_id.split(".")[0];
              const isOn = e.state === "on";
              const friendlyName = (e.attributes.friendly_name as string) ?? e.entity_id;
              return (
                <Card key={e.entity_id}>
                  <CardContent>
                    <div className="flex items-center gap-3">
                      <div className={`size-9 rounded-full flex items-center justify-center shrink-0 ${isOn ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>
                        {domain === "light" ? <Lightbulb className="size-4.5" /> : <Home className="size-4.5" />}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="font-medium text-sm truncate">{friendlyName}</div>
                        <div className="text-xs text-muted-foreground">{e.state}</div>
                      </div>
                      {domain === "light" || domain === "switch" ? (
                        <Button
                          size="icon"
                          variant="ghost"
                          className="size-8 shrink-0"
                          onClick={() => handleControl(e.entity_id, isOn ? "turn_off" : "turn_on")}
                        >
                          {isOn ? <PowerOff className="size-4" /> : <Power className="size-4" />}
                        </Button>
                      ) : null}
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        </>
      )}
    </RoleGuard>
  );
}
