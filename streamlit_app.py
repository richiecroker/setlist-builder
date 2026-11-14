import io
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st
from pypdf import PdfReader, PdfWriter

ROOT_FOLDER_ID = "0B4fd8LwUUi4YYzJ2ZDFwRzJZTlk"
RESOURCE_KEY = "0-A7jhk6BuC3Tdr-PDudWCTg"  # the value after resourcekey= in the share link
API_KEY = st.secrets["drive_api_key"]
API = "https://www.googleapis.com/drive/v3/files"
HEADERS = {"X-Goog-Drive-Resource-Keys": f"{ROOT_FOLDER_ID}/{RESOURCE_KEY}"}

st.set_page_config(page_title="Music book builder", layout="wide")


def list_all(query, fields):
    items, token = [], None
    while True:
        params = {
            "q": query,
            "fields": f"nextPageToken, files({fields})",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "key": API_KEY,
        }
        if token:
            params["pageToken"] = token
        t = time.time()
        r = requests.get(API, params=params, headers=HEADERS, timeout=30)
        print(f"Drive request: {r.status_code} in {time.time() - t:.1f}s", flush=True)
        if not r.ok:
            raise RuntimeError(f"Drive API error {r.status_code}: {r.text}")
        data = r.json()
        items += data["files"]
        token = data.get("nextPageToken")
        if not token:
            return items


@st.cache_data(ttl=600, show_spinner="Reading Drive...")
def load_library():
    t0 = time.time()
    folders = list_all(
        f"'{ROOT_FOLDER_ID}' in parents "
        "and mimeType='application/vnd.google-apps.folder' and trashed=false",
        "id,name",
    )

    def folder_files(folder):
        files = list_all(
            f"'{folder['id']}' in parents and trashed=false", "id,name,mimeType"
        )
        return folder["name"], files

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(folder_files, folders))

    rows, skipped, no_separator = [], [], []
    for song, files in results:
        for f in files:
            if not f["name"].lower().endswith(".pdf"):
                skipped.append((song, f["name"], f["mimeType"]))
                continue
            stem = re.sub(r"\.pdf$", "", f["name"], flags=re.I)
            pieces = re.split(r"\s+[-–—]\s+", stem)
            if len(pieces) < 2:
                no_separator.append((song, f["name"]))
                continue
            rows.append(
                {"song": song, "part": pieces[-1].strip(), "file_id": f["id"]}
            )

    per_folder = Counter(len(files) for _, files in results)
    fewest = sorted(results, key=lambda x: len(x[1]))[:10]

    diag = {
        "folders": len(folders),
        "files_total": sum(len(files) for _, files in results),
        "parsed": len(rows),
        "files_per_folder": dict(sorted(per_folder.items())),
        "fewest_files": {
            song: [f["name"] for f in files] for song, files in fewest
        },
        "skipped_not_pdf_name": skipped[:15],
        "skipped_count": len(skipped),
        "no_separator": no_separator[:15],
        "no_separator_count": len(no_separator),
        "seconds": round(time.time() - t0, 1),
    }
    print(diag, flush=True)
    return pd.DataFrame(rows, columns=["song", "part", "file_id"]), diag


def download_pdf(file_id):
    r = requests.get(
        f"{API}/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true", "key": API_KEY},
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    return io.BytesIO(r.content)


def build_book(selected: pd.DataFrame) -> bytes:
    bar = st.progress(0.0, text="Downloading...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        buffers = []
        for i, buf in enumerate(pool.map(download_pdf, selected["file_id"]), start=1):
            buffers.append(buf)
            bar.progress(i / len(selected), text=f"Downloaded {i}/{len(selected)}")

    writer = PdfWriter()
    for song, buf in zip(selected["song"], buffers):
        writer.append(PdfReader(buf), outline_item=song)
    out = io.BytesIO()
    writer.write(out)
    bar.empty()
    return out.getvalue()


st.title("Music book builder")

try:
    lib, diag = load_library()
except RuntimeError as e:
    st.error(str(e))
    st.stop()

with st.expander("Diagnostics", expanded=lib.empty):
    st.write(diag)

if lib.empty:
    st.warning("No parts found. See Diagnostics above for the skipped files.")
    st.stop()

part_counts = lib["part"].value_counts()
parts = sorted(part_counts.index, key=str.lower)
part = st.selectbox(
    "Part",
    parts,
    index=parts.index(part_counts.idxmax()),
    format_func=lambda p: f"{p} ({part_counts[p]} songs)",
)
available = lib[lib["part"] == part].sort_values("song", key=lambda s: s.str.lower())

table = pd.DataFrame(
    {
        "Include": False,
        "Order": range(1, len(available) + 1),
        "Song": available["song"].values,
    },
    index=available.index,
)

edited = st.data_editor(
    table,
    key=f"editor_{part}",
    hide_index=True,
    disabled=["Song"],
    column_config={
        "Include": st.column_config.CheckboxColumn(),
        "Order": st.column_config.NumberColumn(min_value=1, step=1),
    },
)

chosen = edited[edited["Include"]].sort_values("Order", kind="stable")
st.caption(f"{len(chosen)} of {len(available)} songs selected")

missing = sorted(set(lib["song"]) - set(available["song"]))
if missing:
    with st.expander(f"{len(missing)} songs have no '{part}' part"):
        st.write(missing)

if st.button("Build PDF", disabled=chosen.empty, type="primary"):
    selected = available.loc[chosen.index]
    st.session_state["book"] = (part, build_book(selected))

if "book" in st.session_state:
    book_part, data = st.session_state["book"]
    st.download_button(
        "Download book",
        data=data,
        file_name=f"Music book - {book_part}.pdf",
        mime="application/pdf",
    )
