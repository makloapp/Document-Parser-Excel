import { Download } from "lucide-react";
import type { OcrJobResult } from "@workspace/api-client-react/src/generated/api.schemas";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";

interface ResultsTableProps {
  job: OcrJobResult;
}

export function ResultsTable({ job }: ResultsTableProps) {
  const formatCurrency = (val?: number | null) => {
    if (val === null || val === undefined) return "-";
    return new Intl.NumberFormat("sk-SK", { style: "currency", currency: "EUR" }).format(val);
  };

  const handleDownload = () => {
    fetch('/api/ocr/download/' + job.jobId)
      .then((r) => r.blob())
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'doklady.xlsx';
        a.click();
      });
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b pb-4">
        <div>
          <CardTitle>Výsledky extrakcie</CardTitle>
          <p className="text-sm text-muted-foreground mt-1">Súbor: {job.fileName}</p>
        </div>
        <Button onClick={handleDownload} className="gap-2" data-testid="button-download-excel">
          <Download className="w-4 h-4" />
          Stiahni Excel
        </Button>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow className="bg-muted/50 whitespace-nowrap">
                <TableHead>Súbor</TableHead>
                <TableHead>Doklad</TableHead>
                <TableHead>Stav</TableHead>
                <TableHead>Dátum</TableHead>
                <TableHead>Sadzba DPH</TableHead>
                <TableHead className="text-right">Základ DPH</TableHead>
                <TableHead className="text-right">DPH</TableHead>
                <TableHead className="text-right">Obrat DPH</TableHead>
                <TableHead className="text-right">Zaokrúhlenie</TableHead>
                <TableHead className="text-right">Spolu s DPH</TableHead>
                <TableHead className="text-right">Suma na úhradu</TableHead>
                <TableHead>Popis položky</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {job.rows.map((row, i) => (
                <TableRow key={i} className="whitespace-nowrap">
                  <TableCell className="font-medium text-xs truncate max-w-[150px]" title={row.nazovSuboru}>{row.nazovSuboru}</TableCell>
                  <TableCell>{row.doklad || "-"}</TableCell>
                  <TableCell>
                    <Badge variant={row.stav === "OK" ? "default" : "secondary"}>
                      {row.stav}
                    </Badge>
                  </TableCell>
                  <TableCell>{row.datumVystavenia}</TableCell>
                  <TableCell>{row.sadzbaDph}</TableCell>
                  <TableCell className="text-right">{formatCurrency(row.zakladDph)}</TableCell>
                  <TableCell className="text-right">{formatCurrency(row.dph)}</TableCell>
                  <TableCell className="text-right">{formatCurrency(row.obratDph)}</TableCell>
                  <TableCell className="text-right">{formatCurrency(row.zaokruhlenie)}</TableCell>
                  <TableCell className="text-right font-medium">{formatCurrency(row.spoluSDph)}</TableCell>
                  <TableCell className="text-right font-bold text-primary">{formatCurrency(row.sumaNaUhradu)}</TableCell>
                  <TableCell className="text-xs truncate max-w-[200px]" title={row.popisNajvacsejPolozky}>{row.popisNajvacsejPolozky}</TableCell>
                </TableRow>
              ))}
              {job.rows.length === 0 && (
                <TableRow>
                  <TableCell colSpan={12} className="text-center py-8 text-muted-foreground">
                    Žiadne dáta na zobrazenie
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
