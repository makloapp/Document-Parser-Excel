import { useState, useCallback, useRef } from "react";
import { UploadCloud, Loader2, Archive, X } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

interface UploadZoneProps {
  onUpload: (file: File) => void;
  isUploading: boolean;
  uploadProgress?: { current: number; total: number } | null;
}

export function UploadZone({ onUpload, isUploading, uploadProgress }: UploadZoneProps) {
  const [isDragging, setIsDragging] = useState(false);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragging(false);
      if (isUploading) return;
      const file = e.dataTransfer.files?.[0];
      if (file && file.name.toLowerCase().endsWith(".zip")) {
        setPendingFile(file);
      }
    },
    [isUploading]
  );

  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      setPendingFile(file);
    }
    e.target.value = "";
  }, []);

  const handleSubmit = useCallback(() => {
    if (!pendingFile || isUploading) return;
    onUpload(pendingFile);
    setPendingFile(null);
  }, [pendingFile, isUploading, onUpload]);

  const progressLabel = uploadProgress
    ? `Spracovávam ${uploadProgress.current} / ${uploadProgress.total} dokladov…`
    : "Rozbaľujem ZIP a spracovávam doklady…";

  return (
    <div className="space-y-3">
      <Card>
        <CardContent className="p-0">
          <label
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            className={`
              relative flex flex-col items-center justify-center w-full h-44
              border-2 border-dashed rounded-lg cursor-pointer transition-colors
              ${isDragging ? "border-primary bg-primary/5" : "border-muted-foreground/20 hover:bg-muted/50"}
              ${isUploading ? "opacity-50 pointer-events-none" : ""}
            `}
          >
            <div className="flex flex-col items-center justify-center pt-5 pb-6 text-center px-4">
              {isUploading ? (
                <>
                  <Loader2 className="w-10 h-10 text-primary animate-spin mb-3" />
                  <p className="text-sm font-medium text-foreground">{progressLabel}</p>
                </>
              ) : (
                <>
                  <Archive className="w-10 h-10 text-muted-foreground mb-3" />
                  <p className="mb-1 text-sm font-medium text-foreground">
                    Potiahnite ZIP súbor sem alebo kliknite pre výber
                  </p>
                  <p className="text-xs text-muted-foreground">
                    ZIP archív s JPG/PNG dokladmi — spracujeme všetky naraz
                  </p>
                </>
              )}
            </div>
            <input
              ref={inputRef}
              type="file"
              className="hidden"
              accept=".zip,application/zip"
              onChange={handleChange}
              disabled={isUploading}
              data-testid="input-file-upload"
            />
          </label>
        </CardContent>
      </Card>

      {pendingFile && (
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-3 min-w-0">
                <Archive className="w-5 h-5 text-primary shrink-0" />
                <div className="min-w-0">
                  <p className="text-sm font-medium truncate" title={pendingFile.name}>
                    {pendingFile.name}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {(pendingFile.size / 1024 / 1024).toFixed(1)} MB
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  onClick={() => setPendingFile(null)}
                  className="text-muted-foreground hover:text-destructive transition-colors"
                  aria-label="Odstrániť"
                >
                  <X className="w-4 h-4" />
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={isUploading}
                  className="px-4 py-1.5 text-sm font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
                  data-testid="button-process"
                >
                  Spracovať
                </button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
