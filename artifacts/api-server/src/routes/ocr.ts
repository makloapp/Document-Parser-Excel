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
  limits: { fileSize: 200 * 1024 * 1024 },
  fileFilter: (_req, file, cb) => {
    const allowed = [".jpg", ".jpeg", ".png", ".zip"];
    const ext = path.extname(file.originalname).toLowerCase();
    if (allowed.includes(ext)) {
      cb(null, true);
    } else {
      cb(new Error("Podporované sú iba ZIP archívy alebo JPG/PNG súbory."));
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

async function extractZipImages(zipPath: string, destDir: string): Promise<string[]> {
  return new Promise((resolve, reject) => {
    const script = `
import sys, zipfile, pathlib, os

zip_path = sys.argv[1]
dest_dir = pathlib.Path(sys.argv[2])
dest_dir.mkdir(parents=True, exist_ok=True)
exts = {'.jpg', '.jpeg', '.png'}
extracted = []
with zipfile.ZipFile(zip_path, 'r') as z:
    for info in z.infolist():
        if info.is_dir():
            continue
        name = pathlib.Path(info.filename)
        if name.suffix.lower() not in exts:
            continue
        # flatten — use only the filename, avoid path traversal
        safe_name = name.name
        out_path = dest_dir / safe_name
        # deduplicate names
        counter = 1
        while out_path.exists():
            out_path = dest_dir / f"{name.stem}_{counter}{name.suffix}"
            counter += 1
        with z.open(info) as src, open(out_path, 'wb') as dst:
            dst.write(src.read())
        extracted.append(str(out_path))
print('\\n'.join(extracted))
`;
    const py = spawn("python3", ["-c", script, zipPath, destDir]);
    let stdout = "";
    let stderr = "";
    py.stdout.on("data", (d: Buffer) => { stdout += d.toString(); });
    py.stderr.on("data", (d: Buffer) => { stderr += d.toString(); });
    py.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`Rozbalenie ZIP zlyhalo: ${stderr.slice(-300)}`));
        return;
      }
      const files = stdout.split("\n").map(l => l.trim()).filter(Boolean);
      if (files.length === 0) {
        reject(new Error("ZIP neobsahuje žiadne JPG/PNG obrázky."));
        return;
      }
      resolve(files);
    });
    py.on("error", reject);
  });
}

function sseWrite(res: Response, event: Record<string, unknown>) {
  res.write(`data: ${JSON.stringify(event)}\n\n`);
}

router.post(
  "/ocr/process",
  upload.single("file"),
  async (req: Request, res: Response) => {
    if (!req.file) {
      res.status(400).json({ error: "Žiadny súbor nebol nahraný." });
      return;
    }

    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.setHeader("X-Accel-Buffering", "no");
    res.flushHeaders();

    const jobId = uuidv4();
    const excelPath = path.join(EXCEL_DIR, `${jobId}.xlsx`);
    const t0 = Date.now();
    const uploadedFile = req.file;
    const isZip = path.extname(uploadedFile.originalname).toLowerCase() === ".zip";
    const extractDir = path.join(UPLOAD_DIR, jobId);

    const allRows: unknown[] = [];
    let totalReceipts = 0;
    let validReceipts = 0;
    let imageFiles: string[] = [];

    try {
      sseWrite(res, { type: "status", message: "Rozbaľujem ZIP…" });

      if (isZip) {
        imageFiles = await extractZipImages(uploadedFile.path, extractDir);
      } else {
        imageFiles = [uploadedFile.path];
      }

      const total = imageFiles.length;
      sseWrite(res, { type: "total", total });

      for (let i = 0; i < imageFiles.length; i++) {
        const imgPath = imageFiles[i];
        const fileName = path.basename(imgPath);
        sseWrite(res, { type: "progress", current: i + 1, total, fileName });

        const tempExcel = path.join(EXCEL_DIR, `${jobId}_part${i}.xlsx`);
        const result = await runOcrScript(imgPath, tempExcel);
        allRows.push(...result.rows);
        totalReceipts += result.totalReceipts;
        validReceipts += result.validReceipts;
        try { fs.unlinkSync(tempExcel); } catch {}
      }

      sseWrite(res, { type: "status", message: "Vytváram Excel…" });
      await buildCombinedExcel(allRows, excelPath);

      const record: JobRecord = {
        jobId,
        fileName: uploadedFile.originalname,
        fileCount: imageFiles.length,
        totalReceipts,
        validReceipts,
        processedAt: new Date().toISOString(),
        excelPath,
        rows: allRows,
        processingTimeMs: Date.now() - t0,
      };
      jobStore.set(jobId, record);

      sseWrite(res, {
        type: "complete",
        jobId,
        fileName: record.fileName,
        fileCount: record.fileCount,
        rows: record.rows,
        totalReceipts: record.totalReceipts,
        validReceipts: record.validReceipts,
        processingTimeMs: record.processingTimeMs,
      });
      res.end();
    } catch (err) {
      req.log.error({ err }, "OCR processing failed");
      sseWrite(res, {
        type: "error",
        message: err instanceof Error ? err.message : "Neznáma chyba pri spracovaní.",
      });
      res.end();
    } finally {
      try { fs.unlinkSync(uploadedFile.path); } catch {}
      try { fs.rmSync(extractDir, { recursive: true, force: true }); } catch {}
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
