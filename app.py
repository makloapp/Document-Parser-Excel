import streamlit as st
import tempfile
import subprocess
import json
import sys
import zipfile
import pandas as pd
import cv2
import numpy as np
from pathlib import Path
from io import BytesIO

APP_VERSION = "v_11.06.2026_15:46"

st.set_page_config(page_title="Spracovanie skenov dokladov", layout="centered")

st.title("Spracovanie skenov dokladov")
st.caption(APP_VERSION)

SUPPORTED_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"]
VIDEO_EXTENSIONS = [".mp4", ".mov", ".avi", ".mkv", ".webm"]


def run_ocr_for_files(input_files, tmpdir):
    results = []
    excel_files = []

    progress = st.progress(0)
    status_text = st.empty()

    total_files = len(input_files)

    for index, file_item in enumerate(input_files, start=1):
        original_name = file_item["original_name"]
        input_path = file_item["path"]
        output_path = tmpdir / f"vystup_{index}.xlsx"

        status_text.info(f"Spracúvam {index}/{total_files}: {original_name}")

        result = subprocess.run(
            [sys.executable, "scripts/ocr_process.py", str(input_path), str(output_path)],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            results.append({
                "file": original_name,
                "success": False,
                "error": result.stderr or result.stdout or "Neznáma chyba"
            })
        else:
            parsed_output = None

            if result.stdout.strip():
                try:
                    parsed_output = json.loads(result.stdout)
                except Exception:
                    parsed_output = result.stdout

            if output_path.exists():
                excel_files.append({
                    "source_file": original_name,
                    "excel_path": output_path
                })

            results.append({
                "file": original_name,
                "success": True,
                "parsed_output": parsed_output
            })

        progress.progress(index / total_files)

    status_text.success("Spracovanie dokončené.")
    return results, excel_files


def create_combined_excel(excel_files, one_row_per_source=False):
    combined_sheets = {}

    for excel_item in excel_files:
        source_file = excel_item["source_file"]
        excel_path = excel_item["excel_path"]

        sheets = pd.read_excel(excel_path, sheet_name=None)

        for sheet_name, df in sheets.items():
            if df.empty:
                continue

            if "nazovSuboru" in df.columns:
                df["nazovSuboru"] = source_file
            elif "Názov súboru" in df.columns:
                df["Názov súboru"] = source_file
            elif "Zdrojový súbor" in df.columns:
                df["Zdrojový súbor"] = source_file
            else:
                df.insert(0, "nazovSuboru", source_file)

            if sheet_name not in combined_sheets:
                combined_sheets[sheet_name] = []

            combined_sheets[sheet_name].append(df)

    if not combined_sheets:
        return None

    output_buffer = BytesIO()

    with pd.ExcelWriter(output_buffer, engine="openpyxl") as writer:
        for sheet_name, dataframes in combined_sheets.items():
            combined_df = pd.concat(dataframes, ignore_index=True)
            safe_sheet_name = str(sheet_name)[:31]

            if one_row_per_source and safe_sheet_name == "Doklady":
                source_col = None
                for candidate_col in ["Názov súboru", "nazovSuboru", "Zdrojový súbor"]:
                    if candidate_col in combined_df.columns:
                        source_col = candidate_col
                        break

                if source_col:
                    score_cols = [
                        "Stav",
                        "Dátum vystavenia",
                        "Sadzba DPH",
                        "Základ DPH",
                        "DPH",
                        "Suma na úhradu",
                        "Spolu s DPH",
                        "Obrat DPH",
                        "Text",
                    ]

                    def row_score(row):
                        score = 0

                        stav = str(row.get("Stav", "")).lower()
                        if "ok" in stav:
                            score += 100
                        if "blok nenajdeny" in stav or "blok nenájdený" in stav:
                            score -= 100

                        for col in score_cols:
                            if col in combined_df.columns and pd.notna(row.get(col)) and str(row.get(col)).strip():
                                score += 1

                        text_value = str(row.get("Text", "")) if "Text" in combined_df.columns else ""
                        score += min(len(text_value), 80) / 1000

                        return score

                    combined_df["_riadok_score"] = combined_df.apply(row_score, axis=1)
                    combined_df = (
                        combined_df
                        .sort_values([source_col, "_riadok_score"], ascending=[True, False])
                        .drop_duplicates(subset=[source_col], keep="first")
                        .drop(columns=["_riadok_score"])
                        .reset_index(drop=True)
                    )

            if safe_sheet_name == "Doklady":
                control_cols_to_drop = [
                    col for col in combined_df.columns
                    if str(col).startswith("Unnamed")
                    or str(col).strip() in [
                        "Check súčet DPH",
                        "Kontrola súčtu",
                        "Check DPH",
                        "Check úhrady",
                        "Kontrola",
                    ]
                ]

                if control_cols_to_drop:
                    combined_df = combined_df.drop(columns=control_cols_to_drop, errors="ignore")

                visible_cols = [
                    "Názov súboru",
                    "Doklad",
                    "Stav",
                    "Dátum vystavenia",
                    "Základ DPH",
                    "DPH",
                    "Spolu s DPH",
                    "Text",
                    "Zaokrúhlenie",
                    "Suma na úhradu",
                    "Sadzba DPH",
                    "Obrat DPH",
                ]

                debug_source_cols = [
                    "Zdroj úhrady",
                    "Zdroj DPH",
                    "Zdroj zaokrúhlenia",
                    "Zdroj dátumu",
                    "Všetky dátumy OCR",
                ]

                debug_source_values = {}

                for col in debug_source_cols:
                    if col in combined_df.columns:
                        debug_source_values[col] = combined_df[col].fillna("").astype(str).tolist()
                    else:
                        debug_source_values[col] = [""] * len(combined_df)

                for col in visible_cols:
                    if col not in combined_df.columns:
                        combined_df[col] = ""

                combined_df = combined_df[visible_cols]

                def to_money(value):
                    if pd.isna(value):
                        return None

                    if isinstance(value, (int, float)):
                        return float(value)

                    txt = str(value).strip()
                    txt = txt.replace("€", "")
                    txt = txt.replace("\u00a0", "")
                    txt = txt.replace(" ", "")

                    if not txt:
                        return None

                    if "," in txt:
                        txt = txt.replace(".", "")
                        txt = txt.replace(",", ".")

                    try:
                        return float(txt)
                    except Exception:
                        return None

                check_dph_values = []
                check_uhrady_values = []
                kontrola_values = []

                tolerance = 0.02

                for _, row in combined_df.iterrows():
                    zaklad = to_money(row.get("Základ DPH"))
                    dph = to_money(row.get("DPH"))
                    obrat = to_money(row.get("Obrat DPH"))

                    spolu = to_money(row.get("Spolu s DPH"))
                    zaokruhlenie = to_money(row.get("Zaokrúhlenie"))
                    suma_na_uhradu = to_money(row.get("Suma na úhradu"))

                    check_dph = None
                    if zaklad is not None and dph is not None and obrat is not None:
                        check_dph = round(zaklad + dph - obrat, 2)

                    check_uhrady = None
                    if suma_na_uhradu is not None and spolu is not None and zaokruhlenie is not None:
                        check_uhrady = round(suma_na_uhradu - spolu - zaokruhlenie, 2)

                    check_dph_values.append(check_dph)
                    check_uhrady_values.append(check_uhrady)

                    has_any_check = check_dph is not None or check_uhrady is not None

                    if not has_any_check:
                        kontrola_values.append("")
                    elif (
                        (check_dph is not None and abs(check_dph) > tolerance)
                        or
                        (check_uhrady is not None and abs(check_uhrady) > tolerance)
                    ):
                        kontrola_values.append("Chyba")
                    else:
                        kontrola_values.append("OK")

                combined_df["Check DPH"] = check_dph_values
                combined_df["Check úhrady"] = check_uhrady_values
                combined_df["Kontrola"] = kontrola_values

                for col in debug_source_cols:
                    combined_df[col] = debug_source_values[col]

            combined_df.to_excel(writer, sheet_name=safe_sheet_name, index=False)

            if safe_sheet_name == "Doklady":
                ws = writer.book[safe_sheet_name]

                widths = {
                    "A": 42,
                    "B": 10,
                    "C": 18,
                    "D": 18,
                    "E": 16,
                    "F": 14,
                    "G": 16,
                    "H": 60,
                    "I": 16,
                    "J": 16,
                    "K": 14,
                    "L": 16,
                    "M": 16,
                    "N": 16,
                    "O": 14,
                    "P": 55,
                    "Q": 80,
                    "R": 55,
                    "S": 70,
                    "T": 100,
                }

                for col, width in widths.items():
                    ws.column_dimensions[col].width = width

                for col_letter in ["E", "F", "G", "I", "J", "L", "M", "N"]:
                    for cell in ws[col_letter][1:]:
                        cell.number_format = '#,##0.00 €'

    return output_buffer.getvalue()


def show_results_and_download(results, excel_files, one_row_per_source=False):
    st.subheader("Výsledky spracovania")

    for item in results:
        if item["success"]:
            st.success(f"{item['file']} bol spracovaný.")
        else:
            st.error(f"Spracovanie zlyhalo: {item['file']}")
            st.code(item["error"])

    if not excel_files:
        st.warning("Nevytvoril sa žiadny Excel súbor.")
        return

    try:
        combined_excel = create_combined_excel(excel_files, one_row_per_source=one_row_per_source)
    except Exception as e:
        st.error(f"Nepodarilo sa vytvoriť spoločný Excel: {e}")
        return

    if combined_excel:
        st.success("Spoločný Excel bol vytvorený.")

        st.download_button(
            label="Stiahnuť spoločný Excel",
            data=combined_excel,
            file_name="spolocny_vystup.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.warning("Nebolo čo spojiť do spoločného Excelu.")


def load_files_from_zip(uploaded_zip, tmpdir):
    zip_path = tmpdir / "doklady.zip"
    extract_dir = tmpdir / "rozbalene"
    extract_dir.mkdir(parents=True, exist_ok=True)

    zip_path.write_bytes(uploaded_zip.getbuffer())

    input_files = []

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for item in zip_ref.infolist():
            if item.is_dir():
                continue

            original_name = Path(item.filename).name
            suffix = Path(original_name).suffix.lower()

            if suffix not in SUPPORTED_EXTENSIONS:
                continue

            target_path = extract_dir / original_name

            counter = 1
            while target_path.exists():
                stem = Path(original_name).stem
                suffix = Path(original_name).suffix
                target_path = extract_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            with zip_ref.open(item) as source, open(target_path, "wb") as target:
                target.write(source.read())

            input_files.append({
                "original_name": target_path.name,
                "path": target_path
            })

    return input_files


def load_uploaded_image_files(uploaded_files, tmpdir):
    input_dir = tmpdir / "nahrane_subory"
    input_dir.mkdir(parents=True, exist_ok=True)

    input_files = []

    for index, uploaded_file in enumerate(uploaded_files, start=1):
        original_name = Path(uploaded_file.name).name
        suffix = Path(original_name).suffix.lower()

        if suffix not in SUPPORTED_EXTENSIONS:
            continue

        target_path = input_dir / original_name

        if target_path.exists():
            target_path = input_dir / f"{Path(original_name).stem}_{index}{suffix}"

        target_path.write_bytes(uploaded_file.getbuffer())

        input_files.append({
            "original_name": target_path.name,
            "path": target_path
        })

    return input_files



def rotate_video_frame(frame, rotation_mode):
    if rotation_mode == "Bez otočenia":
        return frame

    if rotation_mode == "90° doprava":
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

    if rotation_mode == "90° doľava":
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

    if rotation_mode == "180°":
        return cv2.rotate(frame, cv2.ROTATE_180)

    if rotation_mode == "Auto na výšku - 90° doprava":
        h, w = frame.shape[:2]
        if w > h:
            return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        return frame

    if rotation_mode == "Auto na výšku - 90° doľava":
        h, w = frame.shape[:2]
        if w > h:
            return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return frame

    return frame



def format_video_timestamp(seconds):
    seconds = max(0, int(round(seconds)))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours:02d}h{minutes:02d}m{secs:02d}s"

    return f"{minutes:02d}m{secs:02d}s"

def create_blocks_zip(input_files):
    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for index, file_item in enumerate(input_files, start=1):
            source_path = Path(file_item["path"])
            original_name = file_item.get("original_name") or source_path.name

            if not source_path.exists():
                continue

            zip_file.write(source_path, arcname=original_name)

    return zip_buffer.getvalue()

def frame_difference(frame_a, frame_b):
    gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)

    gray_a = cv2.resize(gray_a, (320, 240))
    gray_b = cv2.resize(gray_b, (320, 240))

    diff = cv2.absdiff(gray_a, gray_b)
    return float(np.mean(diff))


def frame_sharpness(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def extract_receipt_frames_from_video(
    video_path,
    output_dir,
    sample_fps=4.0,
    stable_diff_threshold=20.0,
    min_stable_seconds=0.5,
    min_gap_seconds=1.5,
    max_frames=80,
    rotation_mode="Bez otočenia",
    video_prefix="video"
):
    output_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video_path))

    if not cap.isOpened():
        raise ValueError("Video sa nepodarilo otvoriť.")

    source_fps = cap.get(cv2.CAP_PROP_FPS)
    if not source_fps or source_fps <= 0:
        source_fps = 25.0

    frame_step = max(1, int(source_fps / sample_fps))
    min_stable_samples = max(2, int(min_stable_seconds * sample_fps))
    min_gap_samples = max(1, int(min_gap_seconds * sample_fps))

    previous_frame = None
    stable_segment = []
    extracted = []
    last_saved_sample_index = -999999

    frame_index = 0
    sample_index = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        frame = rotate_video_frame(frame, rotation_mode)

        if frame_index % frame_step != 0:
            frame_index += 1
            continue

        if previous_frame is None:
            previous_frame = frame
            frame_index += 1
            sample_index += 1
            continue

        diff = frame_difference(previous_frame, frame)

        if diff <= stable_diff_threshold:
            stable_segment.append((sample_index, frame.copy(), frame_sharpness(frame)))
        else:
            if len(stable_segment) >= min_stable_samples:
                segment_center_index = stable_segment[len(stable_segment) // 2][0]

                if segment_center_index - last_saved_sample_index >= min_gap_samples:
                    best_sample_index, best_frame, _sharpness = max(
                        stable_segment,
                        key=lambda item: item[2]
                    )

                    timestamp = format_video_timestamp(best_sample_index / sample_fps)
                    output_path = output_dir / f"{video_prefix}_blok_{len(extracted) + 1:03d}_{timestamp}.jpg"
                    cv2.imwrite(str(output_path), best_frame)

                    extracted.append({
                        "original_name": output_path.name,
                        "path": output_path,
                        "video_time_seconds": best_sample_index / sample_fps
                    })

                    last_saved_sample_index = best_sample_index

                    if len(extracted) >= max_frames:
                        break

            stable_segment = []

        previous_frame = frame
        frame_index += 1
        sample_index += 1

    if len(stable_segment) >= min_stable_samples and len(extracted) < max_frames:
        segment_center_index = stable_segment[len(stable_segment) // 2][0]

        if segment_center_index - last_saved_sample_index >= min_gap_samples:
            best_sample_index, best_frame, _sharpness = max(
                stable_segment,
                key=lambda item: item[2]
            )

            timestamp = format_video_timestamp(best_sample_index / sample_fps)
            output_path = output_dir / f"{video_prefix}_blok_{len(extracted) + 1:03d}_{timestamp}.jpg"
            cv2.imwrite(str(output_path), best_frame)

            extracted.append({
                "original_name": output_path.name,
                "path": output_path,
                "video_time_seconds": best_sample_index / sample_fps
            })

    cap.release()
    return extracted


st.write("Vyber spôsob spracovania dokladov.")

mode = st.radio(
    "Zdroj dokladov",
    [
        "ZIP alebo JPG/PDF súbory",
        "VIDEO súbor"
    ],
    horizontal=False
)

if mode == "ZIP alebo JPG/PDF súbory":
    st.write("Nahraj ZIP súbor alebo jednotlivé JPG, PNG, TIFF, WEBP/PDF doklady. Aplikácia vytvorí jeden spoločný Excel.")
    st.write("Načítanie môže trvať niekoľko minút.")
    st.write("Výsledok závisí od kvality obrázkov.")

    upload_type = st.radio(
        "Typ nahrávania",
        ["ZIP súbor", "Jednotlivé súbory"],
        horizontal=True
    )

    if upload_type == "ZIP súbor":
        uploaded_zip = st.file_uploader(
            "Vyber ZIP súbor s dokladmi",
            type=["zip"]
        )

        if uploaded_zip is not None:
            st.info(f"Nahraný ZIP súbor: {uploaded_zip.name}")

            if st.button("Spracovať ZIP"):
                with tempfile.TemporaryDirectory() as tmpdir_raw:
                    tmpdir = Path(tmpdir_raw)

                    input_files = load_files_from_zip(uploaded_zip, tmpdir)

                    if not input_files:
                        st.error("V ZIP súbore sa nenašli podporované doklady.")
                    else:
                        st.info(f"Počet nájdených dokladov v ZIP: {len(input_files)}")
                        results, excel_files = run_ocr_for_files(input_files, tmpdir)
                        show_results_and_download(results, excel_files, one_row_per_source=True)

    else:
        uploaded_files = st.file_uploader(
            "Vyber JPG, PNG, TIFF, WEBP alebo PDF súbory",
            type=["jpg", "jpeg", "png", "pdf", "tif", "tiff", "webp"],
            accept_multiple_files=True
        )

        if uploaded_files:
            st.info(f"Počet nahraných súborov: {len(uploaded_files)}")

            if st.button("Spracovať súbory"):
                with tempfile.TemporaryDirectory() as tmpdir_raw:
                    tmpdir = Path(tmpdir_raw)

                    input_files = load_uploaded_image_files(uploaded_files, tmpdir)

                    if not input_files:
                        st.error("Nenašli sa podporované súbory.")
                    else:
                        results, excel_files = run_ocr_for_files(input_files, tmpdir)
                        show_results_and_download(results, excel_files, one_row_per_source=True)

else:
    st.write("Nahraj video, v ktorom sú bločky snímané zľava doprava. Nad každým bločkom sa na chvíľu zastav.")
    st.write("Aplikácia z videa vyberie stabilné zábery, uloží ich ako JPG a následne ich spracuje cez OCR.")

    uploaded_videos = st.file_uploader(
        "Vyber jeden alebo viac VIDEO súborov",
        type=["mp4", "mov", "avi", "mkv", "webm"],
        accept_multiple_files=True
    )

    with st.expander("Nastavenia delenia videa"):
        sample_fps = st.slider("Koľkokrát za sekundu kontrolovať video", 1.0, 8.0, 4.0, 0.5)
        stable_diff_threshold = st.slider("Citlivosť zastavenia kamery", 1.0, 35.0, 20.0, 0.5)
        min_stable_seconds = st.slider("Minimálna dĺžka zastavenia nad bločkom", 0.2, 3.0, 0.5, 0.1)
        min_gap_seconds = st.slider("Minimálny odstup medzi dvoma bločkami", 0.5, 5.0, 1.5, 0.1)
        max_frames = st.slider("Maximálny počet vybraných bločkov z videa", 5, 200, 80, 5)
        rotation_mode = st.selectbox(
            "Otočenie JPG záberov z videa",
            [
                "Bez otočenia",
                "Auto na výšku - 90° doprava",
                "Auto na výšku - 90° doľava",
                "90° doprava",
                "90° doľava",
                "180°"
            ],
            index=1
        )

    if st.session_state.get("last_blocks_zip"):
        st.download_button(
            label=f"Znova stiahnuť posledný ZIP s JPG blokmi ({st.session_state.get('last_blocks_zip_count', '?')} súborov)",
            data=st.session_state["last_blocks_zip"],
            file_name=st.session_state.get("last_blocks_zip_name", "vybrane_jpg_bloky.zip"),
            mime="application/zip",
            key="download_blocks_zip_persistent"
        )

    if st.session_state.get("last_output_excel"):
        st.download_button(
            label=f"Znova stiahnuť posledný výstupný Excel ({st.session_state.get('last_output_excel_count', '?')} riadkov)",
            data=st.session_state["last_output_excel"],
            file_name=st.session_state.get("last_output_excel_name", "doklady_vystup.xlsx"),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_output_excel_persistent"
        )

    if uploaded_videos:
        import hashlib

        video_items = []
        seen_hashes = {}

        for video_index, uploaded_video in enumerate(uploaded_videos, start=1):
            video_bytes = uploaded_video.getvalue()
            video_hash = hashlib.sha1(video_bytes).hexdigest()[:10]

            original_name = uploaded_video.name or f"video_{video_index:02d}.mp4"
            video_suffix = Path(original_name).suffix.lower() or ".mp4"

            safe_video_stem = Path(original_name).stem
            safe_video_stem = "".join(ch if ch.isalnum() or ch in ["-", "_"] else "_" for ch in safe_video_stem)
            safe_video_stem = safe_video_stem[:60] or f"video_{video_index:02d}"

            video_items.append({
                "index": video_index,
                "name": original_name,
                "suffix": video_suffix,
                "safe_stem": safe_video_stem,
                "bytes": video_bytes,
                "size": len(video_bytes),
                "hash": video_hash,
            })

            seen_hashes.setdefault(video_hash, []).append(original_name)

        st.info(f"Počet nahraných video súborov: {len(video_items)}")

        with st.expander("Kontrola nahraných videí"):
            for item in video_items:
                st.write(
                    f'{item["index"]}. {item["name"]} | '
                    f'{item["size"] / (1024 * 1024):.2f} MB | '
                    f'ID: {item["hash"]}'
                )

            duplicate_groups = [names for names in seen_hashes.values() if len(names) > 1]
            if duplicate_groups:
                st.warning("Niektoré nahrané videá majú rovnaký obsah. Skontroluj, či nebol rovnaký súbor nahraný viackrát.")
                for names in duplicate_groups:
                    st.write(", ".join(names))

        if st.button("Spracovať VIDEO"):
            with tempfile.TemporaryDirectory() as tmpdir_raw:
                tmpdir = Path(tmpdir_raw)

                all_input_files = []

                for item in video_items:
                    video_index = item["index"]
                    video_prefix = f'video_{video_index:02d}_{item["hash"]}_{item["safe_stem"]}'

                    video_path = tmpdir / f'{video_prefix}{item["suffix"]}'
                    video_path.write_bytes(item["bytes"])

                    extracted_dir = tmpdir / "video_jpg" / video_prefix

                    with st.spinner(f'Rozdeľujem video {video_index}/{len(video_items)}: {item["name"]}'):
                        try:
                            input_files = extract_receipt_frames_from_video(
                                video_path=video_path,
                                output_dir=extracted_dir,
                                sample_fps=sample_fps,
                                stable_diff_threshold=stable_diff_threshold,
                                min_stable_seconds=min_stable_seconds,
                                min_gap_seconds=min_gap_seconds,
                                max_frames=max_frames,
                                rotation_mode=rotation_mode,
                                video_prefix=video_prefix
                            )
                        except Exception as e:
                            st.error(f'Video sa nepodarilo rozdeliť: {item["name"]} — {e}')
                            input_files = []

                    if not input_files:
                        st.warning(f'Z videa {item["name"]} sa nepodarilo vybrať žiadne stabilné zábery.')
                    else:
                        st.success(f'Z videa {item["name"]} bolo vybraných {len(input_files)} JPG záberov.')
                        all_input_files.extend(input_files)

                if not all_input_files:
                    st.error("Zo žiadneho videa sa nepodarilo vybrať stabilné zábery.")
                    st.write("Skús video, kde sa nad každým bločkom zastavíš dlhšie, alebo uprav citlivosť zastavenia.")
                else:
                    st.success(f"Celkovo bolo vybraných {len(all_input_files)} JPG záberov zo všetkých videí.")

                    blocks_zip = create_blocks_zip(all_input_files)

                    st.session_state["last_blocks_zip"] = blocks_zip
                    st.session_state["last_blocks_zip_name"] = "vybrane_jpg_bloky.zip"
                    st.session_state["last_blocks_zip_count"] = len(all_input_files)

                    st.download_button(
                        label="Stiahnuť všetky vybrané JPG bloky ako ZIP",
                        data=st.session_state["last_blocks_zip"],
                        file_name=st.session_state["last_blocks_zip_name"],
                        mime="application/zip",
                        key="download_blocks_zip_after_processing"
                    )

                    with st.expander("Ukážka vybraných JPG záberov"):
                        for file_item in all_input_files[:30]:
                            st.image(str(file_item["path"]), caption=file_item["original_name"], use_container_width=True)

                    results, excel_files = run_ocr_for_files(all_input_files, tmpdir)

                    output_excel = create_combined_excel(
                        excel_files,
                        one_row_per_source=True
                    )

                    if hasattr(output_excel, "getvalue"):
                        output_excel = output_excel.getvalue()

                    st.session_state["last_output_excel"] = output_excel
                    st.session_state["last_output_excel_name"] = "doklady_vystup.xlsx"
                    st.session_state["last_output_excel_count"] = len(results)

                    show_results_and_download(results, excel_files, one_row_per_source=True)
