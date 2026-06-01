import { useState, useCallback } from "react";
import { useProcessReceipt, useListJobs, getListJobsQueryKey } from "@workspace/api-client-react";
import type { OcrJobResult, OcrJobSummary } from "@workspace/api-client-react/src/generated/api.schemas";
import { useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/hooks/use-toast";
import { UploadZone } from "@/components/upload-zone";
import { ResultsTable } from "@/components/results-table";
import { JobHistory } from "@/components/job-history";

export default function Home() {
  const [currentJob, setCurrentJob] = useState<OcrJobResult | null>(null);
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const processMutation = useProcessReceipt({
    mutation: {
      onSuccess: (data) => {
        setCurrentJob(data);
        queryClient.invalidateQueries({ queryKey: getListJobsQueryKey() });
        toast({
          title: "Spracovanie úspešné",
          description: `Extrahovali sme dáta z dokladu ${data.fileName}.`,
        });
      },
      onError: (err) => {
        toast({
          title: "Chyba pri spracovaní",
          description: err?.error || "Nepodarilo sa spracovať doklad.",
          variant: "destructive",
        });
      },
    },
  });

  const handleFileUpload = useCallback(
    (file: File) => {
      const form = new FormData();
      form.append("file", file);
      processMutation.mutate({ data: form });
    },
    [processMutation]
  );

  return (
    <div className="min-h-screen bg-background pb-12">
      <header className="bg-card border-b">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded bg-primary text-primary-foreground flex items-center justify-center font-bold">
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
            />

            {currentJob && (
              <div className="space-y-4">
                <ResultsTable job={currentJob} />
              </div>
            )}
          </div>
          
          <div className="lg:col-span-1">
            <JobHistory onSelectJob={(job) => {
              // The API doesn't have a getJob endpoint based on the spec provided.
              // So if they click a history item, we might not have the full data unless we fetched it.
              // We'll just show a toast for now or we would need an endpoint.
              // Assuming clicking history would need an API endpoint which isn't there, we'll keep the UI simple.
            }} />
          </div>
        </div>
      </main>
    </div>
  );
}
