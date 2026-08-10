import { useState } from "react";
import { Home, Lightbulb, Power, PowerOff } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useQuery } from "@tanstack/react-query";
import { fetchAPI } from "@/api/client";
import { toast } from "sonner";

interface HAType {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
}

export default function SmartHomePage() {
  const [filter, setFilter] = useState("");
  const { data, isLoading, isError } = useQuery<{ entities: HAType[] }>({
    queryKey: ["home", "status"],
    queryFn: () => fetchAPI("/home/status"),
    refetchInterval: 15000,
  });

  const entities = data?.entities ?? [];
  const domains = [...new Set(entities.map((e) => e.entity_id.split(".")[0]))];
  const filtered = filter
    ? entities.filter((e) => e.entity_id.startsWith(filter + "."))
    : entities;

  const handleControl = async (entityId: string, service: string) => {
    try {
      await fetchAPI("/home/control", {
        method: "POST",
        body: JSON.stringify({ entity_id: entityId, service }),
      });
      toast.success(`${entityId} → ${service}`);
    } catch {
      toast.error("控制失败");
    }
  };

  return (
    <>
      <PageHeader title="智能家居" description={`${entities.length} 个设备`} />

      <div className="flex gap-2 mb-4 flex-wrap">
        <Button size="sm" variant={filter === "" ? "default" : "outline"} onClick={() => setFilter("")}>全部</Button>
        {domains.map((d) => (
          <Button key={d} size="sm" variant={filter === d ? "default" : "outline"} onClick={() => setFilter(d)}>
            {d}
          </Button>
        ))}
      </div>

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-20 rounded-lg" />)}
        </div>
      ) : entities.length === 0 ? (
        <EmptyState title="暂无设备" description="连接 Home Assistant 以管理智能设备" icon={<Home className="size-10" />} />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {filtered.map((e) => {
            const domain = e.entity_id.split(".")[0];
            const isOn = e.state === "on";
            const friendlyName = (e.attributes.friendly_name as string) ?? e.entity_id;
            return (
              <Card key={e.entity_id} className="p-4">
                <div className="flex items-center gap-3">
                  <div className={`size-8 rounded-full flex items-center justify-center ${isOn ? "bg-yellow-400/20 text-yellow-600" : "bg-muted text-muted-foreground"}`}>
                    {domain === "light" ? <Lightbulb className="size-4" /> : <Home className="size-4" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm truncate">{friendlyName}</div>
                    <div className="text-xs text-muted-foreground">{e.state}</div>
                  </div>
                  {domain === "light" || domain === "switch" ? (
                    <Button
                      size="icon"
                      variant="ghost"
                      className="size-8"
                      onClick={() => handleControl(e.entity_id, isOn ? "turn_off" : "turn_on")}
                    >
                      {isOn ? <PowerOff className="size-4" /> : <Power className="size-4" />}
                    </Button>
                  ) : null}
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </>
  );
}