import io
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st
from pypdf import PdfReader, PdfWriter

ROOT_FOLDER_ID = "https://drive.google.com/drive/u/0/folders/0B4fd8LwUUi4YYzJ2ZDFwRzJZTlk"
st.write(repr(ROOT_FOLDER_ID), len(ROOT_FOLDER_ID))
API_KEY = st.secrets["drive_api_key"]
API = "https://www.googleapis.com/drive/v3/files"

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
        r = requests.get(API, params=params, timeout=30)
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
    print(f"Found {len(folders)} folders in {time.time() - t0:.1f}s", flush=True)
    folder_names = {f["id"]: f["name"] for f in folders}
    ids = list(folder_names)

    def query_chunk(chunk):
        parents_q = " or ".join(f"'{fid}' in parents" for fid in chunk)
        return list_all(
            f"({parents_q}) and mimeType='application/pdf' and trashed=false",
            "id,name,parents",
        )

    chunks = [ids[i : i + 10] for i in range(0, len(ids), 10)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        pdfs = [p for result in pool.map(query_chunk, chunks) for p in result]
    print(f"Found {len(pdfs)} PDFs in {time.time() - t0:.1f}s total", flush=True)

    rows = []
    for p in pdfs:
        parent = next((x for x in p.get("parents", []) if x in folder_names), None)
        if parent is None:
            continue
        stem = re.sub(r"\.pdf$", "", p["name"], flags=re.I)
        _, sep, part = stem.rpartition(" - ")
        if not sep:
            continue
        rows.append(
            {"song": folder_names[parent], "part": part.strip(), "file_id": p["id"]}
        )
    return pd.DataFrame(rows, columns=["song", "part", "file_id"])


def download_pdf(file_id):
    r = requests.get(
        f"{API}/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true", "key": API_KEY},
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
    lib = load_library()
except RuntimeError as e:
    st.error(str(e))
    st.stop()

if lib.empty:
    st.warning(
        "No parts found. Check the folder ID and that the folder is shared "
        "with 'anyone with the link'."
    )
    st.stop()

part = st.selectbox("Part", sorted(lib["part"].unique(), key=str.lower))
available = lib[lib["part"] == part].sort_values("song", key=lambda s: s.str.lower())

table = pd.DataFrame(
    {
        "Include": False,
        "Order": range(1, len(available) + 1),
        "Song": available["song"].values,
    }
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
    selected = chosen.merge(
        available.rename(columns={"song": "Song"}), on="Song", how="left"
    ).rename(columns={"Song": "song"})
    st.session_state["book"] = (part, build_book(selected))

if "book" in st.session_state:
    book_part, data = st.session_state["book"]
    st.download_button(
        "Download book",
        data=data,
        file_name=f"Music book - {book_part}.pdf",
        mime="application/pdf",
    )
