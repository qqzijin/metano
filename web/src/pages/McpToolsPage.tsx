import { useState } from "react";
import { Wrench, Search, Globe } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useMcpTools, useWebSearch } from "@/api/hooks";
import { toast } from "sonner";

export default function McpToolsPage() {
  const { data, isLoading } = useMcpTools();
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<any>(null);
  const webSearchMut = useWebSearch();

  const tools = data?.tools ?? [];

  const handleWebSearch = async () => {
    if (!searchQuery.trim()) return;
    try {
      const result = await webSearchMut.mutateAsync(searchQuery);
      setSearchResult(result);
      toast.success("搜索完成");
    } catch {
      toast.error("搜索失败");
    }
  };

  return (
    <>
      <PageHeader title="MCP 工具" description={`可用 ${tools.length} 个工具`} />

      {/* Web Search */}
      <Card className="mb-4">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <Globe className="size-4" /> Tavily 网页搜索
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex gap-2">
            <Input
              placeholder="搜索关键词..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="flex-1"
              onKeyDown={(e) => e.key === "Enter" && handleWebSearch()}
            />
            <Button onClick={handleWebSearch} disabled={webSearchMut.isPending}>
              <Search className="size-4 mr-1" /> 搜索
            </Button>
          </div>

          {searchResult && (
            <div className="space-y-3">
              {searchResult.answer && (
                <div className="bg-primary/5 border border-primary/20 rounded-md p-3">
                  <p className="text-sm font-medium mb-1">摘要</p>
                  <p className="text-sm">{searchResult.answer}</p>
                </div>
              )}
              {(searchResult.results ?? []).map((r: any, i: number) => (
                <div key={i} className="bg-muted rounded-md p-3">
                  <div className="font-medium text-sm mb-1">{r.title}</div>
                  <p className="text-xs text-muted-foreground mb-1">{r.snippet}</p>
                  <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-xs text-primary hover:underline">
                    {r.url}
                  </a>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Tool Registry */}
      {isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-24 rounded-lg" />)}
        </div>
      ) : tools.length === 0 ? (
        <EmptyState title="暂无MCP工具" description="启动MCP服务器后工具将自动注册" icon={<Wrench className="size-10" />} />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {tools.map((t) => (
            <Card key={t.name}>
              <CardContent className="p-4">
                <div className="flex items-center gap-2 mb-2">
                  <Wrench className="size-4 text-muted-foreground" />
                  <span className="font-medium text-sm font-mono">{t.name}</span>
                  <Badge variant={t.source === "internal" ? "default" : "secondary"} className="text-[10px]">
                    {t.source === "internal" ? "内置" : "外部"}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2">{t.description || "无描述"}</p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}