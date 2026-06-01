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

router.post(
  "/ocr/process",
  upload.single("file"),
  async (req: Request, res: Response) => {
    if (!req.file) {
      res.status(400).json({ error: "Žiadny súbor nebol nahraný." });
      return;
    }

    const jobId = uuidv4();
    const excelPath = path.join(EXCEL_DIR, `${jobId}.xlsx`);
    const t0 = Date.now();

    try {
      const result = await runOcrScript(req.file.path, excelPath);

      const record: JobRecord = {
        jobId,
        fileName: req.file.originalname,
        totalReceipts: result.totalReceipts,
        validReceipts: result.validReceipts,
        processedAt: new Date().toISOString(),
        excelPath,
        rows: result.rows,
        processingTimeMs: Date.now() - t0,
      };
      jobStore.set(jobId, record);

      try {
        fs.unlinkSync(req.file.path);
      } catch {}

      res.json({
        jobId,
        fileName: record.fileName,
        rows: record.rows,
        totalReceipts: record.totalReceipts,
        validReceipts: record.validReceipts,
        processingTimeMs: record.processingTimeMs,
      });
    } catch (err) {
      try {
        fs.unlinkSync(req.file.path);
      } catch {}
      req.log.error({ err }, "OCR processing failed");
      res
        .status(500)
        .json({
          error:
            err instanceof Error
              ? err.message
              : "Neznáma chyba pri spracovaní.",
        });
    }
  },
);

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
  const excelName = `doklady_${safeName}.xlsx`;
  res.download(job.excelPath, excelName);
});

router.get("/ocr/jobs", (_req: Request, res: Response) => {

  const jobs = Array.from(jobStore.values())
    .sort(
      (a, b) =>
        new Date(b.processedAt).getTime() - new Date(a.processedAt).getTime(),
    )
    .slice(0, 20)
    .map(({ jobId, fileName, totalReceipts, validReceipts, processedAt }) => ({
      jobId,
      fileName,
      totalReceipts,
      validReceipts,
      processedAt,
    }));

  res.json(jobs);
});

export default router;
