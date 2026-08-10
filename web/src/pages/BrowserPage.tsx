import { useState } from "react";
import { Camera, Globe, Search } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useBrowserSearch, useBrowserBrowse, useBrowserScreenshot } from "@/api/hooks";

export default function BrowserPage() {
  const [url, setUrl] = useState("");
  const [mode, setMode] = useState("static");
  const [searchQ, setSearchQ] = useState("");
  const [shotUrl, setShotUrl] = useState("");

  const browseMut = useBrowserBrowse();
  const searchMut = useBrowserSearch();
  const shotMut = useBrowserScreenshot();

  return (
    <>
      <PageHeader title="浏览器" description="Web 自动化工具" />

      <Tabs defaultValue="browse" className="space-y-4">
        <TabsList>
          <TabsTrigger value="browse">浏览</TabsTrigger>
          <TabsTrigger value="screenshot">截图</TabsTrigger>
          <TabsTrigger value="search">搜索</TabsTrigger>
        </TabsList>

        <TabsContent value="browse">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">浏览网址</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex flex-col sm:flex-row gap-2">
                <Input placeholder="https://example.com" value={url} onChange={(e) => setUrl(e.target.value)} className="w-full sm:flex-1" />
                <div className="flex gap-2 shrink-0">
                  <Select value={mode} onValueChange={(v) => { if (v) setMode(v); }}>
                    <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="static">静态</SelectItem>
                      <SelectItem value="dynamic">动态</SelectItem>
                      <SelectItem value="stealth">隐身</SelectItem>
                    </SelectContent>
                  </Select>
                  <Button disabled={!url.trim() || browseMut.isPending} onClick={() => browseMut.mutate(url)}>
                    <Globe className="size-4 mr-1" /> Go
                  </Button>
                </div>
              </div>
              {browseMut.data && (
                <div className="bg-muted rounded-md p-3 text-sm max-h-80 overflow-auto whitespace-pre-wrap">
                  {browseMut.data.title && <div className="font-medium mb-1">{browseMut.data.title}</div>}
                  {browseMut.data.content?.slice(0, 3000)}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="screenshot">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">截图</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex flex-col sm:flex-row gap-2">
                <Input
                  placeholder="https://example.com"
                  value={shotUrl}
                  onChange={(e) => setShotUrl(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && shotUrl.trim() && !shotMut.isPending && shotMut.mutate(shotUrl)}
                  className="w-full sm:flex-1"
                />
                <Button className="shrink-0" disabled={!shotUrl.trim() || shotMut.isPending} onClick={() => shotMut.mutate(shotUrl)}>
                  <Camera className="size-4 mr-1" /> 截图
                </Button>
              </div>
              {shotMut.isPending && <p className="text-sm text-muted-foreground">正在截图，请稍候...</p>}
              {shotMut.isError && <p className="text-sm text-destructive">{shotMut.error?.message || "截图失败"}</p>}
              {shotMut.data?.error && <p className="text-sm text-destructive">{shotMut.data.error}</p>}
              {shotMut.data?.image && (
                <div className="overflow-auto rounded-md border">
                  <img src={shotMut.data.image} alt={`${shotUrl} 截图`} className="w-full h-auto" />
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="search">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">网页搜索</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex flex-wrap gap-2">
                <Input placeholder="搜索关键词..." value={searchQ} onChange={(e) => setSearchQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && searchMut.mutate(searchQ)} className="flex-1 min-w-[140px]" />
                <Button className="shrink-0" disabled={!searchQ.trim() || searchMut.isPending} onClick={() => searchMut.mutate(searchQ)}>
                  <Search className="size-4 mr-1" /> 搜索
                </Button>
              </div>
              {searchMut.data?.results && (
                <div className="space-y-2">
                  {searchMut.data.results.map((r, i) => (
                    <div key={i} className="p-3 border rounded-md">
                      <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-sm font-medium text-primary hover:underline">{r.title}</a>
                      <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{r.snippet}</p>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </>
  );
}