import { useState } from "react";
import { Save } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import { useConfig } from "@/api/hooks";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchAPI } from "@/api/client";
import { toast } from "sonner";

export default function ConfigPage() {
  const { data, isLoading, isError } = useConfig();
  const qc = useQueryClient();
  const [edited, setEdited] = useState<string | null>(null);

  const configText = edited ?? (data ? JSON.stringify(data, null, 2) : "{}");
  const savedText = data ? JSON.stringify(data, null, 2) : "{}";
  const changed = edited !== null && edited !== savedText;

  const saveMut = useMutation({
    mutationFn: () => {
      const parsed = JSON.parse(edited!);
      return fetchAPI("/config", { method: "PUT", body: JSON.stringify({ config: parsed }) });
    },
    onSuccess: () => {
      toast.success("配置已保存");
      setEdited(null);
      qc.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (e) => toast.error(`保存失败: ${e.message}`),
  });

  return (
    <>
      <PageHeader
        title="配置"
        description="系统配置"
        actions={
          <Button size="sm" disabled={!changed || saveMut.isPending} onClick={() => saveMut.mutate()}>
            <Save className="size-4 mr-1" /> {saveMut.isPending ? "保存中..." : changed ? "保存更改" : "已保存"}
          </Button>
        }
      />

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
        <Skeleton className="h-96 rounded-lg" />
      ) : (
        <Card>
          <CardContent className="p-4">
            <Textarea
              value={configText}
              onChange={(e) => setEdited(e.target.value)}
              spellCheck={false}
              className="font-mono text-xs min-h-[500px] resize-y"
            />
          </CardContent>
        </Card>
      )}
    </>
  );
}