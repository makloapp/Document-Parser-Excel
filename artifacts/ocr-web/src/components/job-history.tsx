import { useListJobs } from "@workspace/api-client-react";
import type { OcrJobSummary } from "@workspace/api-client-react/src/generated/api.schemas";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { FileText, CalendarClock } from "lucide-react";
import { format } from "date-fns";
import { sk } from "date-fns/locale";

interface JobHistoryProps {
  onSelectJob: (job: OcrJobSummary) => void;
}

export function JobHistory({ onSelectJob }: JobHistoryProps) {
  const { data: jobs, isLoading } = useListJobs();

  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle className="text-lg flex items-center gap-2">
          <CalendarClock className="w-5 h-5 text-muted-foreground" />
          História
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4">
        {isLoading ? (
          <div className="space-y-4">
            {[1, 2, 3].map(i => (
              <div key={i} className="animate-pulse flex space-x-4">
                <div className="flex-1 space-y-2 py-1">
                  <div className="h-4 bg-muted rounded w-3/4"></div>
                  <div className="h-3 bg-muted rounded w-1/2"></div>
                </div>
              </div>
            ))}
          </div>
        ) : !jobs || jobs.length === 0 ? (
          <div className="text-center py-6 text-sm text-muted-foreground">
            Žiadne predchádzajúce doklady
          </div>
        ) : (
          <div className="space-y-3">
            {jobs.map((job) => (
              <button
                key={job.jobId}
                onClick={() => onSelectJob(job)}
                className="w-full text-left p-3 rounded-md border bg-card hover:bg-muted/50 transition-colors flex items-start gap-3"
              >
                <FileText className="w-8 h-8 text-primary shrink-0 mt-0.5" />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium truncate" title={job.fileName}>
                    {job.fileName}
                  </p>
                  <div className="flex items-center justify-between mt-1 text-xs text-muted-foreground">
                    <span>{job.validReceipts}/{job.totalReceipts} platných</span>
                    <span>{format(new Date(job.processedAt), "d.MMM HH:mm", { locale: sk })}</span>
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
