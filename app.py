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

APP_VERSION = "v_11.06.2026_09:02"

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

            combined_df.to_excel(writer, sheet_name=safe_sheet_name, index=False)

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
    stable_diff_threshold=12.0,
    min_stable_seconds=0.5,
    min_gap_seconds=1.5,
    max_frames=80,
    rotation_mode="Bez otočenia"
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

                    output_path = output_dir / f"video_blok_{len(extracted) + 1:03d}.jpg"
                    cv2.imwrite(str(output_path), best_frame)

                    extracted.append({
                        "original_name": output_path.name,
                        "path": output_path
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

            output_path = output_dir / f"video_blok_{len(extracted) + 1:03d}.jpg"
            cv2.imwrite(str(output_path), best_frame)

            extracted.append({
                "original_name": output_path.name,
                "path": output_path
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
                        show_results_and_download(results, excel_files)

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
                        show_results_and_download(results, excel_files)

else:
    st.write("Nahraj video, v ktorom sú bločky snímané zľava doprava. Nad každým bločkom sa na chvíľu zastav.")
    st.write("Aplikácia z videa vyberie stabilné zábery, uloží ich ako JPG a následne ich spracuje cez OCR.")

    uploaded_video = st.file_uploader(
        "Vyber VIDEO súbor",
        type=["mp4", "mov", "avi", "mkv", "webm"]
    )

    with st.expander("Nastavenia delenia videa"):
        sample_fps = st.slider("Koľkokrát za sekundu kontrolovať video", 1.0, 8.0, 4.0, 0.5)
        stable_diff_threshold = st.slider("Citlivosť zastavenia kamery", 1.0, 35.0, 12.0, 0.5)
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

    if uploaded_video is not None:
        st.info(f"Nahrané video: {uploaded_video.name}")

        if st.button("Spracovať VIDEO"):
            with tempfile.TemporaryDirectory() as tmpdir_raw:
                tmpdir = Path(tmpdir_raw)

                video_suffix = Path(uploaded_video.name).suffix.lower()
                video_path = tmpdir / f"video{video_suffix}"
                video_path.write_bytes(uploaded_video.getbuffer())

                extracted_dir = tmpdir / "video_jpg"

                with st.spinner("Rozdeľujem video na JPG zábery..."):
                    try:
                        input_files = extract_receipt_frames_from_video(
                            video_path=video_path,
                            output_dir=extracted_dir,
                            sample_fps=sample_fps,
                            stable_diff_threshold=stable_diff_threshold,
                            min_stable_seconds=min_stable_seconds,
                            min_gap_seconds=min_gap_seconds,
                            max_frames=max_frames,
                            rotation_mode=rotation_mode
                        )
                    except Exception as e:
                        st.error(f"Video sa nepodarilo rozdeliť: {e}")
                        input_files = []

                if not input_files:
                    st.error("Z videa sa nepodarilo vybrať žiadne stabilné zábery.")
                    st.write("Skús video, kde sa nad každým bločkom zastavíš dlhšie, alebo zníž citlivosť zastavenia.")
                else:
                    st.success(f"Z videa bolo vybraných {len(input_files)} JPG záberov.")

                    blocks_zip = create_blocks_zip(input_files)

                    st.download_button(
                        label="Stiahnuť vybrané JPG bloky ako ZIP",
                        data=blocks_zip,
                        file_name="vybrane_jpg_bloky.zip",
                        mime="application/zip"
                    )

                    with st.expander("Ukážka vybraných JPG záberov"):
                        for file_item in input_files[:20]:
                            st.image(str(file_item["path"]), caption=file_item["original_name"], use_container_width=True)

                    results, excel_files = run_ocr_for_files(input_files, tmpdir)
                    show_results_and_download(results, excel_files, one_row_per_source=True)
