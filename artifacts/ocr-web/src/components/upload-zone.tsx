import { useState, useCallback, useRef } from "react";
import { UploadCloud, Loader2, X, FileImage } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface UploadZoneProps {
  onUpload: (files: File[]) => void;
  isUploading: boolean;
  uploadProgress?: { current: number; total: number } | null;
}

const ALLOWED_TYPES = ["image/jpeg", "image/png"];

function filterImageFiles(fileList: FileList | File[]): File[] {
  return Array.from(fileList).filter((f) => ALLOWED_TYPES.includes(f.type));
}

export function UploadZone({ onUpload, isUploading, uploadProgress }: UploadZoneProps) {
  const [isDragging, setIsDragging] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
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
      const files = filterImageFiles(e.dataTransfer.files);
      if (files.length > 0) {
        setPendingFiles((prev) => {
          const names = new Set(prev.map((f) => f.name));
          return [...prev, ...files.filter((f) => !names.has(f.name))];
        });
      }
    },
    [isUploading]
  );

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (!e.target.files) return;
      const files = filterImageFiles(e.target.files);
      if (files.length > 0) {
        setPendingFiles((prev) => {
          const names = new Set(prev.map((f) => f.name));
          return [...prev, ...files.filter((f) => !names.has(f.name))];
        });
      }
      e.target.value = "";
    },
    []
  );

  const removeFile = useCallback((name: string) => {
    setPendingFiles((prev) => prev.filter((f) => f.name !== name));
  }, []);

  const handleSubmit = useCallback(() => {
    if (pendingFiles.length === 0 || isUploading) return;
    onUpload(pendingFiles);
    setPendingFiles([]);
  }, [pendingFiles, isUploading, onUpload]);

  const progressLabel = uploadProgress
    ? `Spracovávam ${uploadProgress.current} / ${uploadProgress.total}…`
    : "Spracovanie dokladov…";

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
                  <UploadCloud className="w-10 h-10 text-muted-foreground mb-3" />
                  <p className="mb-1 text-sm font-medium text-foreground">
                    Potiahnite súbory sem alebo kliknite pre výber
                  </p>
                  <p className="text-xs text-muted-foreground">
                    JPG alebo PNG — môžete vybrať viacero dokladov naraz
                  </p>
                </>
              )}
            </div>
            <input
              ref={inputRef}
              type="file"
              className="hidden"
              accept="image/jpeg,image/png"
              multiple
              onChange={handleChange}
              disabled={isUploading}
              data-testid="input-file-upload"
            />
          </label>
        </CardContent>
      </Card>

      {pendingFiles.length > 0 && (
        <Card>
          <CardContent className="p-4 space-y-2">
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium text-foreground">
                Vybrané súbory
                <Badge variant="secondary" className="ml-2">{pendingFiles.length}</Badge>
              </span>
              <button
                onClick={handleSubmit}
                disabled={isUploading}
                className="px-4 py-1.5 text-sm font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
                data-testid="button-process"
              >
                Spracovať
              </button>
            </div>
            <ul className="space-y-1 max-h-40 overflow-y-auto pr-1">
              {pendingFiles.map((f) => (
                <li
                  key={f.name}
                  className="flex items-center gap-2 text-xs text-muted-foreground bg-muted/40 rounded px-2 py-1"
                >
                  <FileImage className="w-3.5 h-3.5 shrink-0 text-primary" />
                  <span className="truncate flex-1" title={f.name}>{f.name}</span>
                  <span className="shrink-0 text-muted-foreground/60">
                    {(f.size / 1024).toFixed(0)} kB
                  </span>
                  <button
                    onClick={() => removeFile(f.name)}
                    className="ml-1 text-muted-foreground hover:text-destructive transition-colors"
                    aria-label="Odstrániť"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
