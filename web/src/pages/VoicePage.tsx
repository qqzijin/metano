import { useState } from "react";
import { Volume2 } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useVoiceTTS } from "@/api/hooks";
import { toast } from "sonner";

export default function VoicePage() {
  const [ttsText, setTtsText] = useState("");
  const [voice, setVoice] = useState("zh-CN-YunxiNeural");
  const [rate, setRate] = useState("+0%");
  const [audioPath, setAudioPath] = useState<string | null>(null);

  const ttsMut = useVoiceTTS();

  const handleTTS = async () => {
    if (!ttsText.trim()) return;
    try {
      const result = await ttsMut.mutateAsync({ text: ttsText, voice, rate });
      setAudioPath(result.path);
      toast.success("音频已生成");
    } catch {
      toast.error("语音生成失败");
    }
  };

  return (
    <>
      <PageHeader title="语音" description="TTS 工具" />

      <Tabs defaultValue="tts" className="space-y-4">
        <TabsList>
          <TabsTrigger value="tts">文字转语音</TabsTrigger>
        </TabsList>

        <TabsContent value="tts">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base flex items-center gap-2">
                <Volume2 className="size-4" /> 文字转语音
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <Textarea placeholder="输入要朗读的文字..." value={ttsText} onChange={(e) => setTtsText(e.target.value)} className="min-h-24" />
              <div className="flex gap-2">
                <Select value={voice} onValueChange={(v) => { if (v) setVoice(v); }}>
                  <SelectTrigger className="w-48"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="zh-CN-YunxiNeural">Yunxi (Chinese)</SelectItem>
                    <SelectItem value="en-US-AriaNeural">Aria (English)</SelectItem>
                    <SelectItem value="ja-JP-NanamiNeural">Nanami (Japanese)</SelectItem>
                  </SelectContent>
                </Select>
                <Select value={rate} onValueChange={(v) => { if (v) setRate(v); }}>
                  <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="-20%">慢速</SelectItem>
                    <SelectItem value="+0%">正常</SelectItem>
                    <SelectItem value="+20%">快速</SelectItem>
                  </SelectContent>
                </Select>
                <Button disabled={!ttsText.trim() || ttsMut.isPending} onClick={handleTTS}>
                  <Volume2 className="size-4 mr-1" /> 生成
                </Button>
              </div>
              {audioPath && (
                <div className="bg-muted rounded-md p-3">
                  <p className="text-xs text-muted-foreground mb-2">音频文件: {audioPath}</p>
                  <audio controls className="w-full" src={`/api/voice/file?path=${encodeURIComponent(audioPath)}`} />
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

      </Tabs>
    </>
  );
}