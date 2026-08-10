import { useState } from "react";
import { Cpu, Star, ExternalLink, Plus, Check, X, Pencil, Save } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { StatCard } from "@/components/shared/StatCard";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useModels, useProxyProviders, useModelSetDefault, useProxyAdd, useProxyUpdate } from "@/api/hooks";
import { toast } from "sonner";

// 这些预设依赖外部 API，启用时必须提供 API Key；ollama-local 为本地服务无需 Key。
const NEEDS_API_KEY_PRESETS = new Set(["nvidia-nim", "deepseek", "kimi", "openrouter", "siliconflow"]);

export default function ModelsPage() {
  const { data: modelsData, isLoading: modelsLoading, isError: modelsError } = useModels();
  const { data: proxyData, isLoading: proxyLoading, isError: proxyError } = useProxyProviders();
  const setDefaultMut = useModelSetDefault();
  const addProxyMut = useProxyAdd();
  const updateProxyMut = useProxyUpdate();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", base_url: "", api_key: "", model: "", max_tokens: 4096 });
  const [editingPrice, setEditingPrice] = useState<string | null>(null);
  const [priceForm, setPriceForm] = useState({ input: "", output: "", cache_read: "" });

  const fmtPrice = (v?: number) => (v == null ? "—" : `$${v}/M`);

  const startEditPrice = (p: any) => {
    const pr = p.price ?? {};
    setPriceForm({
      input: pr.input != null ? String(pr.input) : "",
      output: pr.output != null ? String(pr.output) : "",
      cache_read: pr.cache_read != null ? String(pr.cache_read) : "",
    });
    setEditingPrice(p.name);
  };

  const savePrice = async (name: string) => {
    const price: Record<string, number> = {};
    if (priceForm.input !== "") price.input = Number(priceForm.input);
    if (priceForm.output !== "") price.output = Number(priceForm.output);
    if (priceForm.cache_read !== "") price.cache_read = Number(priceForm.cache_read);
    if (Object.keys(price).length === 0) {
      toast.error("至少填写一项价格");
      return;
    }
    try {
      await updateProxyMut.mutateAsync({ name, body: { price } });
      toast.success(`已更新价格: ${name}`);
      setEditingPrice(null);
    } catch (e: any) {
      toast.error(`更新失败: ${e.message ?? "未知错误"}`);
    }
  };

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

  // Presets are display-only; "启用" must register the provider first, then set default.
  const handleEnablePreset = async (p: any) => {
    const payload: { name: string; base_url: string; model: string; api_key?: string } = {
      name: p.name,
      base_url: p.base_url,
      model: p.model,
    };
    // 若预设自带 api_key 则直接使用
    if (p.api_key) payload.api_key = p.api_key;
    // 需要 Key 但未提供时引导到添加表单
    if (NEEDS_API_KEY_PRESETS.has(p.name) && !payload.api_key) {
      setShowForm(true);
      toast.error(`该预设需要 API Key，请先配置`);
      return;
    }
    try {
      await addProxyMut.mutateAsync(payload);
      await setDefaultMut.mutateAsync(p.name);
      toast.success(`已启用并设为默认: ${p.name}`);
    } catch {
      toast.error("启用预设失败");
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
  const isError = modelsError || proxyError;

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

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-6">
        <StatCard label="已配置提供商" value={providers.length} />
        <StatCard label="免费预设" value={presets.length} />
        <StatCard label="当前默认" value={currentDefault || "未设置"} />
      </div>

      {/* Add Provider Form */}
      {showForm && (
        <Card className="mb-6">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">添加模型提供商</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="min-w-0">
                <label className="block text-xs text-muted-foreground mb-1.5">名称 *</label>
                <Input placeholder="如: openai" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </div>
              <div className="min-w-0">
                <label className="block text-xs text-muted-foreground mb-1.5">Base URL *</label>
                <Input placeholder="如: https://api.openai.com/v1" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
              </div>
              <div className="min-w-0">
                <label className="block text-xs text-muted-foreground mb-1.5">API Key</label>
                <Input type="password" placeholder="sk-..." value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
              </div>
              <div className="min-w-0">
                <label className="block text-xs text-muted-foreground mb-1.5">默认模型</label>
                <Input placeholder="如: gpt-4o" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} />
              </div>
              <div className="min-w-0">
                <label className="block text-xs text-muted-foreground mb-1.5">Max Tokens</label>
                <Input type="number" placeholder="4096" value={form.max_tokens} onChange={(e) => setForm({ ...form, max_tokens: parseInt(e.target.value) || 4096 })} />
              </div>
            </div>
            <Button size="sm" onClick={handleAddProxy} disabled={addProxyMut.isPending || !form.name || !form.base_url}>
              <Plus className="size-3.5 mr-1" /> {addProxyMut.isPending ? "添加中..." : "确认添加"}
            </Button>
          </CardContent>
        </Card>
      )}

      {isError ? (
        <div className="text-sm text-destructive">加载失败，请检查服务或刷新重试</div>
      ) : isLoading ? (
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
                    <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center">
                      <div className="flex items-center gap-3 min-w-0 flex-1">
                        <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                          <Cpu className="size-4.5" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="font-medium text-sm flex items-center gap-2 flex-wrap">
                            <span className="truncate">{p.name}</span>
                            {p.is_default && <Badge className="text-[10px]"><Star className="size-3 mr-0.5" /> 默认</Badge>}
                          </div>
                          <div className="text-xs text-muted-foreground mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 min-w-0">
                            {p.model && <span className="shrink-0">模型: {p.model}</span>}
                            {p.base_url && <span className="truncate min-w-0">{p.base_url}</span>}
                          </div>
                          <div className="text-xs mt-1 text-muted-foreground">
                            输入 {fmtPrice(p.price?.input)} · 输出 {fmtPrice(p.price?.output)} · 缓存 {fmtPrice(p.price?.cache_read)}
                          </div>
                          {editingPrice === p.name && (
                            <div className="flex flex-col gap-2 mt-2 sm:flex-row sm:items-center">
                              <div className="grid grid-cols-3 gap-2 w-full sm:w-auto">
                                <Input type="number" step="any" className="h-8 w-full min-w-0" placeholder="输入价" value={priceForm.input} onChange={(e) => setPriceForm({ ...priceForm, input: e.target.value })} />
                                <Input type="number" step="any" className="h-8 w-full min-w-0" placeholder="输出价" value={priceForm.output} onChange={(e) => setPriceForm({ ...priceForm, output: e.target.value })} />
                                <Input type="number" step="any" className="h-8 w-full min-w-0" placeholder="缓存价" value={priceForm.cache_read} onChange={(e) => setPriceForm({ ...priceForm, cache_read: e.target.value })} />
                              </div>
                              <div className="flex gap-2 shrink-0">
                                <Button size="sm" onClick={() => savePrice(p.name)} disabled={updateProxyMut.isPending}>
                                  <Save className="size-3.5 mr-1" /> 保存
                                </Button>
                                <Button size="sm" variant="ghost" onClick={() => setEditingPrice(null)}>取消</Button>
                              </div>
                            </div>
                          )}
                        </div>
                      </div>
                      <div className="flex items-center gap-1.5 shrink-0 flex-wrap">
                        <Button size="sm" variant="outline" onClick={() => startEditPrice(p)}>
                          <Pencil className="size-3.5 mr-1" /> 价格
                        </Button>
                        {!p.is_default && (
                          <Button size="sm" variant="outline" onClick={() => handleSetDefault(p.name)}>
                            <Check className="size-3.5 mr-1" /> 设为默认
                          </Button>
                        )}
                      </div>
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
                      <div className="flex items-center gap-2 mb-1.5 min-w-0">
                        <div className="flex size-7 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                          <ExternalLink className="size-3.5" />
                        </div>
                        <span className="font-medium text-sm truncate min-w-0">{p.name}</span>
                      </div>
                      <div className="text-xs text-muted-foreground space-y-0.5">
                        {p.base_url && <div className="truncate">URL: {p.base_url}</div>}
                        {p.model && <div>默认模型: {p.model}</div>}
                        {p.note && <div className="break-words">{p.note}</div>}
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        className="mt-3"
                        onClick={() => handleEnablePreset(p)}
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
