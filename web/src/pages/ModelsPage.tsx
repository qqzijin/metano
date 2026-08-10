import { useState } from "react";
import { Cpu, Star, ExternalLink, Plus, Check, X } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { StatCard } from "@/components/shared/StatCard";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useModels, useProxyProviders, useModelSetDefault, useProxyAdd } from "@/api/hooks";
import { toast } from "sonner";

export default function ModelsPage() {
  const { data: modelsData, isLoading: modelsLoading } = useModels();
  const { data: proxyData, isLoading: proxyLoading } = useProxyProviders();
  const setDefaultMut = useModelSetDefault();
  const addProxyMut = useProxyAdd();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", base_url: "", api_key: "", model: "", max_tokens: 4096 });

  const providers = modelsData?.providers ?? [];
  const presets = proxyData?.providers ?? [];
  const currentDefault = providers.find((p: any) => p.is_default)?.name ?? "";

  const handleSetDefault = async (name: string) => {
    try {
      await setDefaultMut.mutateAsync(name);
      toast.success(`已切换默认模型: ${name}`);
    } catch {
      toast.error("切换失败");
    }
  };

  const handleAddProxy = async () => {
    if (!form.name || !form.base_url) {
      toast.error("名称和 URL 必填");
      return;
    }
    try {
      await addProxyMut.mutateAsync(form);
      toast.success(`已添加: ${form.name}`);
      setShowForm(false);
      setForm({ name: "", base_url: "", api_key: "", model: "", max_tokens: 4096 });
    } catch (e: any) {
      toast.error(`添加失败: ${e.message ?? "未知错误"}`);
    }
  };

  const isLoading = modelsLoading || proxyLoading;

  return (
    <>
      <PageHeader
        title="模型管理"
        description="管理 AI 模型提供商与免费预设"
        actions={
          <Button size="sm" onClick={() => setShowForm(!showForm)}>
            {showForm ? <X className="size-4 mr-1" /> : <Plus className="size-4 mr-1" />}
            {showForm ? "取消" : "添加提供商"}
          </Button>
        }
      />

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 mb-6">
        <StatCard label="已配置提供商" value={providers.length} />
        <StatCard label="免费预设" value={presets.length} />
        <StatCard label="当前默认" value={currentDefault || "未设置"} />
      </div>

      {/* Add Provider Form */}
      {showForm && (
        <Card className="mb-6">
          <CardContent className="p-4 space-y-3">
            <h4 className="text-sm font-medium">添加模型提供商</h4>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-muted-foreground mb-1">名称 *</label>
                <Input placeholder="如: openai" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </div>
              <div>
                <label className="text-xs text-muted-foreground mb-1">Base URL *</label>
                <Input placeholder="如: https://api.openai.com/v1" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
              </div>
              <div>
                <label className="text-xs text-muted-foreground mb-1">API Key</label>
                <Input type="password" placeholder="sk-..." value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
              </div>
              <div>
                <label className="text-xs text-muted-foreground mb-1">默认模型</label>
                <Input placeholder="如: gpt-4o" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} />
              </div>
              <div>
                <label className="text-xs text-muted-foreground mb-1">Max Tokens</label>
                <Input type="number" placeholder="4096" value={form.max_tokens} onChange={(e) => setForm({ ...form, max_tokens: parseInt(e.target.value) || 4096 })} />
              </div>
            </div>
            <Button size="sm" onClick={handleAddProxy} disabled={addProxyMut.isPending || !form.name || !form.base_url}>
              <Plus className="size-3.5 mr-1" /> {addProxyMut.isPending ? "添加中..." : "确认添加"}
            </Button>
          </CardContent>
        </Card>
      )}

      {isLoading ? (
        <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-24 rounded-lg" />)}</div>
      ) : (
        <>
          {/* Configured Providers */}
          <div className="mb-6">
            <h3 className="text-sm font-medium text-muted-foreground mb-3">已配置提供商</h3>
            {providers.length === 0 ? (
              <EmptyState title="暂无配置" description="点击上方「添加提供商」配置模型" />
            ) : (
              <div className="grid gap-3">
                {providers.map((p: any) => (
                  <Card key={p.name}>
                    <CardContent className="p-4 flex items-center gap-4">
                      <Cpu className="size-5 text-muted-foreground shrink-0" />
                      <div className="flex-1 min-w-0">
                        <div className="font-medium text-sm flex items-center gap-2">
                          {p.name}
                          {p.is_default && <Badge className="text-[10px]"><Star className="size-3 mr-0.5" /> 默认</Badge>}
                        </div>
                        <div className="text-xs text-muted-foreground mt-0.5 flex gap-3">
                          {p.model && <span>模型: {p.model}</span>}
                          {p.base_url && <span className="truncate">{p.base_url}</span>}
                        </div>
                      </div>
                      {!p.is_default && (
                        <Button size="sm" variant="outline" onClick={() => handleSetDefault(p.name)}>
                          <Check className="size-3.5 mr-1" /> 设为默认
                        </Button>
                      )}
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
          </div>

          {/* Free Presets */}
          {presets.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-muted-foreground mb-3">免费模型预设</h3>
              <div className="grid gap-3 sm:grid-cols-2">
                {presets.map((p: any) => (
                  <Card key={p.name}>
                    <CardContent className="p-4">
                      <div className="flex items-center gap-2 mb-1">
                        <ExternalLink className="size-3.5 text-muted-foreground" />
                        <span className="font-medium text-sm">{p.name}</span>
                      </div>
                      <div className="text-xs text-muted-foreground space-y-0.5">
                        {p.base_url && <div className="truncate">URL: {p.base_url}</div>}
                        {p.model && <div>默认模型: {p.model}</div>}
                        {p.note && <div>{p.note}</div>}
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        className="mt-2"
                        onClick={() => handleSetDefault(p.name)}
                        disabled={setDefaultMut.isPending}
                      >
                        <Plus className="size-3.5 mr-1" /> 启用
                      </Button>
                    </CardContent>
                  </Card>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </>
  );
}