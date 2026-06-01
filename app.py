import streamlit as st
import tempfile
import subprocess
import json
import sys
import zipfile
import pandas as pd
from pathlib import Path
from io import BytesIO

st.set_page_config(page_title="OCR dokladov", layout="centered")

st.title("OCR spracovanie dokladov")

st.write("Nahraj ZIP súbor s JPG, PNG, TIFF, WEBP alebo PDF dokladmi. Aplikácia vytvorí jeden spoločný Excel.")

uploaded_zip = st.file_uploader(
    "Vyber ZIP súbor s dokladmi",
    type=["zip"]
)

SUPPORTED_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"]

if uploaded_zip is not None:
    st.info(f"Nahraný ZIP súbor: {uploaded_zip.name}")

    if st.button("Spracovať ZIP"):
        results = []
        excel_files = []

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            zip_path = tmpdir / "doklady.zip"
            extract_dir = tmpdir / "rozbalene"
            extract_dir.mkdir(parents=True, exist_ok=True)

            zip_path.write_bytes(uploaded_zip.getbuffer())

            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_items = zip_ref.infolist()

                input_files = []

                for item in zip_items:
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
                        "original_name": original_name,
                        "path": target_path
                    })

            if not input_files:
                st.error("V ZIP súbore sa nenašli podporované doklady.")
            else:
                st.info(f"Počet nájdených dokladov v ZIP: {len(input_files)}")

                for index, file_item in enumerate(input_files, start=1):
                    original_name = file_item["original_name"]
                    input_path = file_item["path"]
                    output_path = tmpdir / f"vystup_{index}.xlsx"

                    with st.spinner(f"Spracúvam: {original_name}"):
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

                st.subheader("Výsledky spracovania")

                for item in results:
                    if item["success"]:
                        st.success(f"{item['file']} bol spracovaný.")
                    else:
                        st.error(f"Spracovanie zlyhalo: {item['file']}")
                        st.code(item["error"])

                if excel_files:
                    combined_sheets = {}

                    for excel_item in excel_files:
                        source_file = excel_item["source_file"]
                        excel_path = excel_item["excel_path"]

                        try:
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

                        except Exception as e:
                            st.warning(f"Nepodarilo sa načítať Excel zo súboru {source_file}: {e}")

                    if combined_sheets:
                        output_buffer = BytesIO()

                        with pd.ExcelWriter(output_buffer, engine="openpyxl") as writer:
                            for sheet_name, dataframes in combined_sheets.items():
                                combined_df = pd.concat(dataframes, ignore_index=True)
                                safe_sheet_name = str(sheet_name)[:31]
                                combined_df.to_excel(writer, sheet_name=safe_sheet_name, index=False)

                        st.success("Spoločný Excel bol vytvorený.")

                        st.download_button(
                            label="Stiahnuť spoločný Excel",
                            data=output_buffer.getvalue(),
                            file_name="spolocny_vystup.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                    else:
                        st.warning("Nebolo čo spojiť do spoločného Excelu.")
                else:
                    st.warning("Nevytvoril sa žiadny Excel súbor.")