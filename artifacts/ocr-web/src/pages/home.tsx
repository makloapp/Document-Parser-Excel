import { useState, useCallback } from "react";
import { useMutation } from "@tanstack/react-query";
import { getListJobsQueryKey } from "@workspace/api-client-react";
import type { OcrJobResult } from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/hooks/use-toast";
import { UploadZone } from "@/components/upload-zone";
import { ResultsTable } from "@/components/results-table";
import { JobHistory } from "@/components/job-history";

export interface UploadProgress {
  current: number;
  total: number;
  fileName: string;
  statusMessage: string;
}

async function uploadReceiptsStreaming(
  form: FormData,
  onProgress: (p: UploadProgress) => void,
): Promise<OcrJobResult> {
  const res = await fetch("/api/ocr/process", { method: "POST", body: form });

  if (!res.ok || !res.body) {
    const json = await res.json().catch(() => ({}));
    throw new Error((json as { error?: string })?.error ?? `HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const raw = line.slice(6).trim();
      if (!raw) continue;

      let evt: Record<string, unknown>;
      try {
        evt = JSON.parse(raw) as Record<string, unknown>;
      } catch {
        continue;
      }

      if (evt.type === "progress") {
        onProgress({
          current: evt.current as number,
          total: evt.total as number,
          fileName: evt.fileName as string,
          statusMessage: `Spracovávam doklad ${evt.current as number} / ${evt.total as number}`,
        });
      } else if (evt.type === "status") {
        onProgress({
          current: 0,
          total: 0,
          fileName: "",
          statusMessage: evt.message as string,
        });
      } else if (evt.type === "complete") {
        return evt as unknown as OcrJobResult;
      } else if (evt.type === "error") {
        throw new Error((evt.message as string) ?? "Neznáma chyba");
      }
    }
  }

  throw new Error("Spojenie sa skončilo bez výsledku.");
}

export default function Home() {
  const [currentJob, setCurrentJob] = useState<OcrJobResult | null>(null);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const processMutation = useMutation({
    mutationFn: (form: FormData) =>
      uploadReceiptsStreaming(form, (p) => setUploadProgress(p)),
    onSuccess: (data) => {
      setCurrentJob(data);
      setUploadProgress(null);
      queryClient.invalidateQueries({ queryKey: getListJobsQueryKey() });
      const count = data.fileCount ?? 1;
      toast({
        title: "Spracovanie úspešné",
        description:
          count > 1
            ? `Spracovaných ${count} dokladov — nájdených ${data.validReceipts} záznamov.`
            : `Extrahované dáta z dokladu ${data.fileName}.`,
      });
    },
    onError: (err: Error) => {
      setUploadProgress(null);
      toast({
        title: "Chyba pri spracovaní",
        description: err.message ?? "Nepodarilo sa spracovať doklad.",
        variant: "destructive",
      });
    },
  });

  const handleFileUpload = useCallback(
    (file: File) => {
      const form = new FormData();
      setUploadProgress({ current: 0, total: 0, fileName: "", statusMessage: "Nahrávam súbor…" });
      form.append("file", file);
      processMutation.mutate(form);
    },
    [processMutation],
  );

  return (
    <div className="min-h-screen bg-background pb-12">
      <header className="bg-card border-b">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded bg-primary text-primary-foreground flex items-center justify-center font-bold text-xs">
              OCR
            </div>
            <h1 className="font-semibold text-lg">Doklady.app</h1>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-8">
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-8">
          <div className="lg:col-span-3 space-y-8">
            <UploadZone
              onUpload={handleFileUpload}
              isUploading={processMutation.isPending}
              uploadProgress={uploadProgress}
            />

            {currentJob && (
              <div className="space-y-4">
                <ResultsTable job={currentJob} />
              </div>
            )}
          </div>

          <div className="lg:col-span-1">
            <JobHistory onSelectJob={() => {}} />
          </div>
        </div>
      </main>
    </div>
  );
}
