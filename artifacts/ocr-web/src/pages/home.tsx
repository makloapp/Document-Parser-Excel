import { useState, useCallback } from "react";
import { useMutation } from "@tanstack/react-query";
import { getListJobsQueryKey } from "@workspace/api-client-react";
import type { OcrJobResult } from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/hooks/use-toast";
import { UploadZone } from "@/components/upload-zone";
import { ResultsTable } from "@/components/results-table";
import { JobHistory } from "@/components/job-history";

async function uploadReceipts(form: FormData): Promise<OcrJobResult> {
  const res = await fetch("/api/ocr/process", { method: "POST", body: form });
  const json = await res.json();
  if (!res.ok) throw new Error(json?.error ?? `HTTP ${res.status}`);
  return json as OcrJobResult;
}

export default function Home() {
  const [currentJob, setCurrentJob] = useState<OcrJobResult | null>(null);
  const [uploadProgress, setUploadProgress] = useState<{ current: number; total: number } | null>(null);
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const processMutation = useMutation({
    mutationFn: uploadReceipts,
    onSuccess: (data) => {
      setCurrentJob(data);
      setUploadProgress(null);
      queryClient.invalidateQueries({ queryKey: getListJobsQueryKey() });
      const count = data.fileCount ?? 1;
      toast({
        title: "Spracovanie úspešné",
        description:
          count > 1
            ? `Spracovaných ${count} súborov — nájdených ${data.validReceipts} dokladov.`
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
    (files: File[]) => {
      const form = new FormData();
      setUploadProgress({ current: 0, total: files.length });
      if (files.length === 1) {
        form.append("file", files[0]);
      } else {
        files.forEach((f) => form.append("files", f));
      }
      processMutation.mutate(form);
    },
    [processMutation]
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
