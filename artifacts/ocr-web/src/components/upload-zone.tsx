import { useState, useCallback } from "react";
import { UploadCloud, File, Loader2 } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

interface UploadZoneProps {
  onUpload: (file: File) => void;
  isUploading: boolean;
}

export function UploadZone({ onUpload, isUploading }: UploadZoneProps) {
  const [isDragging, setIsDragging] = useState(false);

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
      const file = e.dataTransfer.files?.[0];
      if (file && (file.type === "image/jpeg" || file.type === "image/png")) {
        onUpload(file);
      }
    },
    [onUpload]
  );

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) {
        onUpload(file);
      }
    },
    [onUpload]
  );

  return (
    <Card>
      <CardContent className="p-0">
        <label
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          className={`
            relative flex flex-col items-center justify-center w-full h-48 
            border-2 border-dashed rounded-lg cursor-pointer transition-colors
            ${isDragging ? "border-primary bg-primary/5" : "border-muted-foreground/20 hover:bg-muted/50"}
            ${isUploading ? "opacity-50 pointer-events-none" : ""}
          `}
        >
          <div className="flex flex-col items-center justify-center pt-5 pb-6 text-center px-4">
            {isUploading ? (
              <Loader2 className="w-10 h-10 text-primary animate-spin mb-3" />
            ) : (
              <UploadCloud className="w-10 h-10 text-muted-foreground mb-3" />
            )}
            <p className="mb-2 text-sm font-medium text-foreground">
              {isUploading ? "Spracovanie dokladu..." : "Kliknite pre nahratie dokladu alebo potiahnite súbor"}
            </p>
            {!isUploading && (
              <p className="text-xs text-muted-foreground">Len JPG alebo PNG obrázky</p>
            )}
          </div>
          <input
            type="file"
            className="hidden"
            accept="image/jpeg,image/png"
            onChange={handleChange}
            disabled={isUploading}
            data-testid="input-file-upload"
          />
        </label>
      </CardContent>
    </Card>
  );
}
