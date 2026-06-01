import { Router, type Request, type Response } from "express";
import multer from "multer";
import path from "path";
import fs from "fs";
import { spawn } from "child_process";
import { v4 as uuidv4 } from "uuid";

const router = Router();

const UPLOAD_DIR = path.join(process.cwd(), "tmp", "uploads");
const EXCEL_DIR = path.join(process.cwd(), "tmp", "excel");

[UPLOAD_DIR, EXCEL_DIR].forEach((dir) => {
  fs.mkdirSync(dir, { recursive: true });
});

const storage = multer.diskStorage({
  destination: (_req, _file, cb) => cb(null, UPLOAD_DIR),
  filename: (_req, file, cb) => {
    const ext = path.extname(file.originalname);
    cb(null, `${uuidv4()}${ext}`);
  },
});

const upload = multer({
  storage,
  limits: { fileSize: 30 * 1024 * 1024 },
  fileFilter: (_req, file, cb) => {
    const allowed = [".jpg", ".jpeg", ".png"];
    const ext = path.extname(file.originalname).toLowerCase();
    if (allowed.includes(ext)) {
      cb(null, true);
    } else {
      cb(new Error("Podporované sú iba JPG a PNG súbory."));
    }
  },
});

interface JobRecord {
  jobId: string;
  fileName: string;
  fileCount: number;
  totalReceipts: number;
  validReceipts: number;
  processedAt: string;
  excelPath: string;
  rows: unknown[];
  processingTimeMs: number;
}

const jobStore: Map<string, JobRecord> = new Map();

const OCR_SCRIPT = path.join(
  path.dirname(new URL(import.meta.url).pathname),
  "..",
  "..",
  "..",
  "scripts",
  "ocr_process.py",
);

function runOcrScript(
  filePath: string,
  excelPath: string,
): Promise<{ rows: unknown[]; totalReceipts: number; validReceipts: number }> {
  return new Promise((resolve, reject) => {
    const python = spawn("python3", [OCR_SCRIPT, filePath, excelPath], {
      timeout: 120000,
    });

    let stdout = "";
    let stderr = "";

    python.stdout.on("data", (data: Buffer) => {
      stdout += data.toString();
    });

    python.stderr.on("data", (data: Buffer) => {
      stderr += data.toString();
    });

    python.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`OCR zlyhalo (kód ${code}): ${stderr.slice(-500)}`));
        return;
      }
      try {
        const jsonStart = stdout.indexOf("{");
        const jsonEnd = stdout.lastIndexOf("}");
        if (jsonStart === -1 || jsonEnd === -1) {
          reject(new Error("Neplatný výstup OCR skriptu"));
          return;
        }
        const result = JSON.parse(stdout.slice(jsonStart, jsonEnd + 1)) as {
          rows: unknown[];
          totalReceipts: number;
          validReceipts: number;
        };
        resolve(result);
      } catch (e) {
        reject(new Error(`Chyba pri parsovaní výsledku: ${e}`));
      }
    });

    python.on("error", (err) => {
      reject(new Error(`Chyba pri spúšťaní Python: ${err.message}`));
    });
  });
}

function collectUploadedFiles(req: Request): Express.Multer.File[] {
  const files: Express.Multer.File[] = [];
  if (req.file) {
    files.push(req.file);
  }
  if (req.files) {
    if (Array.isArray(req.files)) {
      files.push(...req.files);
    } else {
      for (const group of Object.values(req.files)) {
        files.push(...group);
      }
    }
  }
  return files;
}

function buildSummaryName(files: Express.Multer.File[]): string {
  if (files.length === 1) return files[0].originalname;
  if (files.length === 2) return `${files[0].originalname}, ${files[1].originalname}`;
  return `${files[0].originalname} (+${files.length - 1} ďalších)`;
}

router.post(
  "/ocr/process",
  upload.fields([
    { name: "file", maxCount: 1 },
    { name: "files", maxCount: 20 },
  ]),
  async (req: Request, res: Response) => {
    const uploadedFiles = collectUploadedFiles(req);

    if (uploadedFiles.length === 0) {
      res.status(400).json({ error: "Žiadny súbor nebol nahraný." });
      return;
    }

    const jobId = uuidv4();
    const excelPath = path.join(EXCEL_DIR, `${jobId}.xlsx`);
    const t0 = Date.now();

    const allRows: unknown[] = [];
    let totalReceipts = 0;
    let validReceipts = 0;
    const tempExcelPaths: string[] = [];

    try {
      for (let i = 0; i < uploadedFiles.length; i++) {
        const file = uploadedFiles[i];
        const tempExcel = path.join(EXCEL_DIR, `${jobId}_part${i}.xlsx`);
        tempExcelPaths.push(tempExcel);

        const result = await runOcrScript(file.path, tempExcel);
        allRows.push(...result.rows);
        totalReceipts += result.totalReceipts;
        validReceipts += result.validReceipts;

        try { fs.unlinkSync(file.path); } catch {}
        try { fs.unlinkSync(tempExcel); } catch {}
      }

      await buildCombinedExcel(allRows, excelPath);

      const record: JobRecord = {
        jobId,
        fileName: buildSummaryName(uploadedFiles),
        fileCount: uploadedFiles.length,
        totalReceipts,
        validReceipts,
        processedAt: new Date().toISOString(),
        excelPath,
        rows: allRows,
        processingTimeMs: Date.now() - t0,
      };
      jobStore.set(jobId, record);

      res.json({
        jobId,
        fileName: record.fileName,
        fileCount: record.fileCount,
        rows: record.rows,
        totalReceipts: record.totalReceipts,
        validReceipts: record.validReceipts,
        processingTimeMs: record.processingTimeMs,
      });
    } catch (err) {
      for (const f of uploadedFiles) {
        try { fs.unlinkSync(f.path); } catch {}
      }
      req.log.error({ err }, "OCR batch processing failed");
      res.status(500).json({
        error:
          err instanceof Error
            ? err.message
            : "Neznáma chyba pri spracovaní.",
      });
    }
  },
);

async function buildCombinedExcel(rows: unknown[], outputPath: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const script = `
import json, sys, pandas as pd, pathlib

rows = json.loads(sys.argv[1])
output = sys.argv[2]

excel_rows = []
for row in rows:
    excel_rows.append({
        "Názov súboru": row.get("nazovSuboru", ""),
        "Doklad": row.get("doklad", ""),
        "Stav": row.get("stav", ""),
        "Dátum vystavenia": row.get("datumVystavenia", ""),
        "Sadzba DPH": row.get("sadzbaDph", ""),
        "Základ DPH": row.get("zakladDph"),
        "DPH": row.get("dph"),
        "Suma na úhradu": row.get("sumaNaUhradu"),
        "Spolu s DPH": row.get("spoluSDph"),
        "Obrat DPH": row.get("obratDph"),
        "Zaokrúhlenie": row.get("zaokruhlenie"),
        "Popis najväčšej položky": row.get("popisNajvacsejPolozky", ""),
    })

df = pd.DataFrame(excel_rows)
cols = ["Názov súboru","Doklad","Stav","Dátum vystavenia","Sadzba DPH","Základ DPH","DPH","Suma na úhradu","Spolu s DPH","Obrat DPH","Zaokrúhlenie","Popis najväčšej položky"]
for c in cols:
    if c not in df.columns:
        df[c] = ""
with pd.ExcelWriter(output, engine="openpyxl") as writer:
    df[cols].to_excel(writer, sheet_name="Doklady", index=False)
    ws = writer.book["Doklady"]
    tolerance = 0.002
    ws["M1"] = "Check súčet DPH"
    ws["N1"] = "Kontrola súčtu"
    for row_idx in range(2, ws.max_row + 1):
        ws[f"M{row_idx}"] = f"=IFERROR(F{row_idx}+G{row_idx}-J{row_idx},\\"\\")".replace('\\"', '"')
        ws[f"N{row_idx}"] = f"=IF(M{row_idx}=\\"\\",\\"\\",IF(ABS(M{row_idx})>{tolerance},\\"Chyba\\",\\"OK\\"))".replace('\\"', '"')
    widths = {"A":34,"B":10,"C":18,"D":18,"E":16,"F":14,"G":12,"H":14,"I":14,"J":14,"K":16,"L":60}
    for col,width in widths.items():
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2, min_col=6, max_col=11):
        for cell in row:
            cell.number_format = '#,##0.00 €'
print("ok")
`;
    const py = spawn("python3", ["-c", script, JSON.stringify(rows), outputPath]);
    let stderr = "";
    py.stderr.on("data", (d: Buffer) => { stderr += d.toString(); });
    py.on("close", (code) => {
      if (code !== 0) reject(new Error(`Excel build failed: ${stderr.slice(-300)}`));
      else resolve();
    });
    py.on("error", reject);
  });
}

router.get("/ocr/download/:jobId", (req: Request, res: Response) => {
  const job = jobStore.get(req.params.jobId as string);
  if (!job) {
    res.status(404).json({ error: "Job nenájdený." });
    return;
  }
  if (!fs.existsSync(job.excelPath)) {
    res.status(404).json({ error: "Excel súbor nenájdený." });
    return;
  }
  const safeName = job.fileName.replace(/[^a-zA-Z0-9._-]/g, "_");
  res.download(job.excelPath, `doklady_${safeName}.xlsx`);
});

router.get("/ocr/jobs", (_req: Request, res: Response) => {
  const jobs = Array.from(jobStore.values())
    .sort(
      (a, b) =>
        new Date(b.processedAt).getTime() - new Date(a.processedAt).getTime(),
    )
    .slice(0, 20)
    .map(({ jobId, fileName, fileCount, totalReceipts, validReceipts, processedAt }) => ({
      jobId,
      fileName,
      fileCount,
      totalReceipts,
      validReceipts,
      processedAt,
    }));

  res.json(jobs);
});

export default router;
